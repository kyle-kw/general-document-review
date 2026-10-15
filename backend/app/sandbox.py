"""Application-owned Linux namespace boundary for all agent tools.

Never resolve or open an agent supplied path on the host. Tool paths and commands
are passed as JSON over stdin to a trusted helper mounted in a fresh namespace.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import sys
import tempfile
from typing import Any

from app.config import Settings


class SandboxUnavailable(RuntimeError):
    pass


class SandboxError(RuntimeError):
    pass


ALLOWED_COMMANDS = frozenset({"python", "python3", "ls", "cat", "head", "tail", "wc", "rg", "officecli"})
OFFICECLI_VERSION = "1.0.157"
OFFICECLI_SHA256 = "f8eefe88dbfa7464bee421e5421efe57fc26bc3180b30e56eef58594c8245c94"
OFFICECLI_BINARY = Path("/usr/local/bin/officecli")
OFFICECLI_COMMANDS = frozenset({
    "help", "--help", "-h", "--version", "--output-schema-crc", "get", "query", "view",
    "validate", "raw", "dump", "create", "set", "add", "remove", "move", "swap", "batch",
    "raw-set", "add-part",
})
OFFICECLI_VIEW_MODES = frozenset({"text", "outline", "stats", "annotated", "issues"})
OFFICECLI_SKILL = Path(__file__).with_name("skills") / "officecli" / "SKILL.md"
OPERATIONS = frozenset({"list_files", "read_file", "write_file", "run_command"})
LIMIT_EXIT_CODE = 125
LIMIT_MARKER = "REVIEW_SANDBOX_LIMIT:"


def officecli_skill_identity() -> str:
    """Bind checkpoints to the fixed executable and the actual review skill."""
    content = hashlib.sha256(OFFICECLI_SKILL.read_bytes())
    for name in ("docx.md", "xlsx.md", "pptx.md"):
        content.update(name.encode())
        content.update((OFFICECLI_SKILL.parent / "upstream" / name).read_bytes())
    return f"officecli-{OFFICECLI_VERSION}:{content.hexdigest()}"


def stage_officecli_skill(workspace: Path) -> dict[str, str]:
    """Stage the review entry point and fixed, read-only syntax references.

    The adapter calls this before starting any agent process in a new workspace.
    All destinations are application constants, never names supplied by agents.
    """
    target = workspace / "input" / "skills" / "officecli"
    target.mkdir(parents=True, exist_ok=False)
    content = OFFICECLI_SKILL.read_bytes()
    (target / "SKILL.md").write_bytes(content)
    references = target / "references"
    references.mkdir()
    for name in ("docx.md", "xlsx.md", "pptx.md"):
        (references / name).write_bytes((OFFICECLI_SKILL.parent / "upstream" / name).read_bytes())
    metadata = {"version": OFFICECLI_VERSION, "path": "input/skills/officecli/SKILL.md",
                "sha256": hashlib.sha256(content).hexdigest(), "identity": officecli_skill_identity()}
    (target / "version.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    return metadata


def validate_officecli_arguments(argv: list[str]) -> None:
    if len(argv) < 2 or argv[1] not in OFFICECLI_COMMANDS:
        raise ValueError("OfficeCLI 仅允许文档检查与临时副本操作，禁止安装、插件、服务和配置命令")
    if "--browser" in argv:
        raise ValueError("OfficeCLI 不允许启动浏览器")
    if argv[1] == "view" and (len(argv) < 4 or argv[3] not in OFFICECLI_VIEW_MODES):
        raise ValueError("OfficeCLI view 仅允许 text、outline、stats、annotated 和 issues")


def cleanup_workspace(workspace: Path) -> None:
    """Delete an app-owned workspace after all of its namespaces have stopped.

    Agent code can chmod its own scratch directories. Restore directory access
    without following links before rmtree, so such modes cannot prevent cleanup.
    The caller must stop processes first; this is not a live-tree traversal API.
    """
    if not workspace.exists():
        return
    if workspace.is_symlink():
        raise SandboxError("拒绝清理符号链接工作区")
    pending = [workspace]
    while pending:
        directory = pending.pop()
        os.chmod(directory, 0o700, follow_symlinks=False)
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
    shutil.rmtree(workspace)


def create_workspace(parent: Path, payload: dict[str, Any]) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="agent-", dir=parent)).resolve()
    try:
        (workspace / "input").mkdir(mode=0o700)
        (workspace / "scratch").mkdir(mode=0o700)
        (workspace / "input" / "request.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return workspace
    except BaseException:
        cleanup_workspace(workspace)
        raise


def _relative_path(value: Any, *, writable: bool = False) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("路径必须为工作区相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", "..", ""} for part in value.split("/")):
        raise ValueError("禁止绝对路径、空路径和路径穿越")
    if path.parts[0] not in ({"scratch"} if writable else {"input", "scratch"}):
        raise ValueError("仅允许 input/ 和 scratch/，写入仅允许 scratch/")
    return value


def validate_operation(operation: str, arguments: dict[str, Any]) -> None:
    if operation not in OPERATIONS or not isinstance(arguments, dict):
        raise ValueError("不支持的沙箱操作")
    if operation == "run_command":
        argv = arguments.get("argv")
        if not isinstance(argv, list) or not argv or len(argv) > 256:
            raise ValueError("argv 必须为非空参数数组")
        if not all(isinstance(arg, str) and "\x00" not in arg for arg in argv):
            raise ValueError("argv 只能包含字符串")
        if argv[0] not in ALLOWED_COMMANDS:
            raise ValueError("只允许 Python、OfficeCLI 和基本文件查看、检索命令")
        if argv[0] == "officecli":
            validate_officecli_arguments(argv)
        if sum(len(arg.encode()) for arg in argv) > 1024 * 1024:
            raise ValueError("命令参数过大")
        _relative_path(arguments.get("cwd", "scratch"), writable=True)
        if set(arguments) - {"argv", "cwd"}:
            raise ValueError("不允许自定义环境、挂载或执行权限")
    else:
        _relative_path(arguments.get("path", "input"), writable=operation == "write_file")
        allowed = {"path", "offset", "limit"} if operation == "read_file" else {"path", "content"} if operation == "write_file" else {"path"}
        if set(arguments) - allowed:
            raise ValueError("未知工具参数")


def _linux_available(settings: Settings) -> Path:
    if sys.platform != "linux":
        raise SandboxUnavailable("审查 worker 需要 Linux/WSL2 的 bubblewrap 原生隔离；不允许无沙箱运行")
    executable = Path(settings.sandbox_bwrap_path)
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise SandboxUnavailable("找不到可执行的 bubblewrap，请安装并配置 SANDBOX_BWRAP_PATH")
    if not Path("/usr/bin/rg").is_file():
        raise SandboxUnavailable("缺少沙箱检索工具 /usr/bin/rg")
    if OFFICECLI_BINARY.is_symlink() or not OFFICECLI_BINARY.is_file() or not os.access(OFFICECLI_BINARY, os.X_OK):
        raise SandboxUnavailable("缺少固定版本 OfficeCLI，请重新构建项目 worker 镜像")
    return executable


def build_command(settings: Settings, workspace: Path) -> list[str]:
    executable = _linux_available(settings)
    # /runtime/venv is a single dependency installation, never its parent /app.
    # Its interpreter must resolve into /usr; other Python layouts fail closed.
    interpreter = Path(sys.executable).resolve()
    if not interpreter.is_relative_to(Path("/usr").resolve()):
        raise SandboxUnavailable("Python 基础运行时必须安装在 /usr 下，请使用项目 Docker 镜像")
    command = [str(executable), "--unshare-user", "--unshare-pid", "--unshare-net", "--unshare-ipc",
               "--unshare-uts", "--new-session", "--die-with-parent", "--as-pid-1",
               "--uid", "1000", "--gid", "1000", "--cap-drop", "ALL", "--clearenv"]
    for source in ("/usr", "/lib", "/lib64"):
        if Path(source).exists():
            command += ["--ro-bind", source, source]
    command += ["--dir", "/runtime", "--ro-bind", str(Path(sys.prefix).resolve()), "/runtime/venv",
                "--ro-bind", str(Path(__file__).with_name("sandbox_exec.py").resolve()), "/runtime/sandbox_exec.py",
                "--proc", "/proc", "--remount-ro", "/proc", "--dev", "/dev",
                "--dir", "/workspace", "--ro-bind", str(workspace / "input"), "/workspace/input",
                "--bind", str(workspace / "scratch"), "/workspace/scratch",
                "--setenv", "PATH", "/runtime/venv/bin:/usr/local/bin:/usr/bin:/bin",
                "--setenv", "HOME", "/workspace/scratch", "--setenv", "TMPDIR", "/workspace/scratch",
                "--setenv", "LANG", "C.UTF-8", "--setenv", "PYTHONIOENCODING", "utf-8",
                "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--chdir", "/workspace/scratch",
                "--remount-ro", "/", "--", "/runtime/venv/bin/python", "-I", "-B", "/runtime/sandbox_exec.py"]
    return command


def _scratch_bytes(root: Path) -> int:
    def count(descriptor: int, depth: int = 0) -> int:
        if depth > 128:
            raise SandboxError("工作区目录深度超过限制")
        total = 0
        with os.scandir(descriptor) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                    total += info.st_size
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                        try:
                            total += count(child, depth + 1)
                        finally:
                            os.close(child)
                except (FileNotFoundError, NotADirectoryError):
                    continue
                except OSError as exc:
                    # A racing symlink is never followed on the host.
                    import errno
                    if exc.errno != errno.ELOOP:
                        raise
        return total

    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return count(descriptor)
    finally:
        os.close(descriptor)


class WorkspaceSandbox:
    def __init__(self, settings: Settings, workspace: Path):
        self.settings = settings
        self.workspace = workspace.resolve()
        self._active: set[asyncio.subprocess.Process] = set()
        self._lock = asyncio.Lock()
        self._closed = False

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            # bwrap and its PID 1 are killed; namespace teardown also kills
            # detached descendants, unlike host process-group cleanup alone.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()

    async def execute(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        validate_operation(operation, arguments)
        async with self._lock:
            if self._closed:
                raise SandboxError("工作区已关闭")
            command = build_command(self.settings, self.workspace)
            request = json.dumps({"operation": operation, "arguments": arguments, "limits": {
                "memory": self.settings.sandbox_memory_mb * 1024 * 1024,
                "officecli_as": self.settings.sandbox_officecli_as_mb * 1024 * 1024,
                "pids": self.settings.sandbox_pids_limit,
                "file": self.settings.sandbox_file_mb * 1024 * 1024,
                "seconds": self.settings.sandbox_command_timeout,
                "output": self.settings.sandbox_output_bytes,
                "workspace": self.settings.sandbox_workspace_mb * 1024 * 1024,
            }}, ensure_ascii=False).encode() + b"\n"
            if len(request) > 4 * 1024 * 1024:
                raise ValueError("工具请求超过限制")
            process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                start_new_session=True,
            )
            self._active.add(process)
            stdout, stderr = bytearray(), bytearray()
            total = 0

            async def read_stream(stream: asyncio.StreamReader, target: bytearray) -> None:
                nonlocal total
                while chunk := await stream.read(16384):
                    total += len(chunk)
                    remaining = max(0, self.settings.sandbox_output_bytes - len(stdout) - len(stderr))
                    target.extend(chunk[:remaining])
                    if total > self.settings.sandbox_output_bytes:
                        raise SandboxError("命令输出超过限制，进程已停止")

            async def quota() -> None:
                while process.returncode is None:
                    if await asyncio.to_thread(_scratch_bytes, self.workspace / "scratch") > self.settings.sandbox_workspace_mb * 1024 * 1024:
                        raise SandboxError("工作区容量超过限制，进程已停止")
                    await asyncio.sleep(0.05)

            tasks = [asyncio.create_task(read_stream(process.stdout, stdout)),
                     asyncio.create_task(read_stream(process.stderr, stderr)), asyncio.create_task(quota())]
            try:
                assert process.stdin is not None
                # Starting/draining a pipe is part of the wall-time bound too:
                # an init failure must not hang a multi-megabyte tool request.
                async with asyncio.timeout(self.settings.sandbox_command_timeout):
                    process.stdin.write(request)
                    await process.stdin.drain()
                    process.stdin.close()
                    await asyncio.gather(process.wait(), *tasks)
            except TimeoutError as exc:
                raise SandboxError("命令执行超时，进程已停止") from exc
            except (BrokenPipeError, ConnectionResetError):
                await process.wait()
            finally:
                await self._stop(process)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self._active.discard(process)
            output = stdout.decode("utf-8", errors="replace")
            error = stderr.decode("utf-8", errors="replace")
            if process.returncode == LIMIT_EXIT_CODE and LIMIT_MARKER in error:
                raise SandboxError(error.split(LIMIT_MARKER, 1)[1].strip())
            if operation == "run_command":
                return {"stdout": output, "stderr": error, "exit_code": process.returncode, "truncated": False}
            if process.returncode != 0:
                raise SandboxError(f"沙箱工具失败：{output or error}")
            try:
                result = json.loads(output)
            except ValueError as exc:
                raise SandboxError(f"沙箱返回无效响应：{error}") from exc
            if not isinstance(result, dict):
                raise SandboxError("沙箱返回非对象响应")
            return result

    async def close(self) -> None:
        self._closed = True
        for process in tuple(self._active):
            await self._stop(process)
        # Workspace belongs to this invocation, not the task or another agent.
        await asyncio.to_thread(cleanup_workspace, self.workspace)


async def preflight(settings: Settings) -> None:
    _linux_available(settings)
    with OFFICECLI_BINARY.open("rb") as binary:
        digest = hashlib.file_digest(binary, "sha256").hexdigest()
    if digest != OFFICECLI_SHA256:
        raise SandboxUnavailable("OfficeCLI 二进制校验失败，请重新构建固定版本 worker 镜像")
    with tempfile.TemporaryDirectory(prefix="document-review-preflight-") as temporary:
        parent = Path(temporary)
        canary = parent / "host-secret-canary"
        canary.write_text("host secret must stay inaccessible", encoding="utf-8")
        workspace = create_workspace(parent, {"preflight": True})
        sandbox = WorkspaceSandbox(settings, workspace)
        try:
            await sandbox.execute("write_file", {"path": "scratch/probe.txt", "content": "ok"})
            result = await sandbox.execute("read_file", {"path": "scratch/probe.txt"})
            if result.get("content") != "ok":
                raise SandboxUnavailable("沙箱读写预检失败")
            script = "\n".join([
                "import os,socket,pathlib,json,ctypes,errno",
                f"assert not pathlib.Path({str(canary)!r}).exists()",
                "assert set(os.environ) <= {'PATH','HOME','TMPDIR','LANG','PYTHONIOENCODING','PYTHONDONTWRITEBYTECODE','LC_CTYPE','PWD'}",
                "assert os.environ['HOME']=='/workspace/scratch'",
                "assert os.environ.get('PWD','/workspace/scratch')=='/workspace/scratch'",
                "assert os.getpid() != 1",
                "assert os.getppid()==1",
                "assert not pathlib.Path('/app').exists()",
                "assert not pathlib.Path('/etc').exists()",
                "assert not pathlib.Path('/root').exists()",
                "assert not pathlib.Path('/home').exists()",
                "libc=ctypes.CDLL(None,use_errno=True)",
                "assert libc.unshare(0x10000000)==-1 and ctypes.get_errno()==errno.EPERM",
                "assert libc.ptrace(0,0,0,0)==-1 and ctypes.get_errno()==errno.EPERM",
                "try:",
                " open('/proc/1/mem','rb')",
                " raise AssertionError('parent memory accessible')",
                "except PermissionError: pass",
                "try:",
                " pathlib.Path('/workspace/input/request.json').write_text('bad')",
                " raise AssertionError('input writable')",
                "except OSError: pass",
                "try:",
                " socket.create_connection(('1.1.1.1',53),timeout=0.2)",
                " raise AssertionError('network accessible')",
                "except OSError: pass",
                "os.symlink('/proc/1/environ','/workspace/scratch/link')",
                "print('isolated')",
            ])
            result = await sandbox.execute("run_command", {"argv": ["python", "-I", "-c", script]})
            if result["exit_code"] != 0 or "isolated" not in result["stdout"]:
                raise SandboxUnavailable(f"Linux 隔离预检失败：{result['stderr'] or result['stdout']}")
            try:
                await sandbox.execute("read_file", {"path": "scratch/link"})
            except SandboxError:
                pass
            else:
                raise SandboxUnavailable("沙箱符号链接防护失败")
            # A normal return from PID 1 must kill even a detached writer.
            detached = "import subprocess; subprocess.Popen(['python','-c',\"import time,pathlib; time.sleep(0.3); pathlib.Path('/workspace/scratch/leaked').write_text('bad')\"],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
            result = await sandbox.execute("run_command", {"argv": ["python", "-c", detached]})
            await asyncio.sleep(0.5)
            if result["exit_code"] != 0 or (workspace / "scratch" / "leaked").exists():
                raise SandboxUnavailable("沙箱后台进程清理预检失败")
            result = await sandbox.execute("run_command", {"argv": ["officecli", "--version"]})
            if result["exit_code"] != 0 or result["stdout"].strip() != OFFICECLI_VERSION:
                raise SandboxUnavailable(f"OfficeCLI 隔离运行预检失败：{result['stderr'] or result['stdout']}")
        except SandboxUnavailable:
            raise
        except Exception as exc:
            raise SandboxUnavailable(f"Linux 原生沙箱预检失败，拒绝无沙箱执行：{exc}") from exc
        finally:
            await sandbox.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="检查真实 Linux Agent 沙箱隔离")
    parser.add_argument("--preflight", action="store_true")
    parser.parse_args()
    asyncio.run(preflight(Settings()))
    print("Linux sandbox preflight passed")


if __name__ == "__main__":
    main()
