import asyncio
import json
from pathlib import Path
import sys

import pytest

from app.config import Settings
from app.sandbox import (
    SandboxError, SandboxUnavailable, WorkspaceSandbox, build_command,
    cleanup_workspace, create_workspace, preflight, validate_operation,
)


@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "input/../../.env", "scratch/../input/file",
                                     "scratch\\x", "C:/secret", "input//x", "input/./x", "input/x\x00"])
def test_tool_paths_reject_escape(path):
    with pytest.raises(ValueError):
        validate_operation("read_file", {"path": path})


@pytest.mark.parametrize("arguments", [
    {"argv": ["sh", "-c", "pwd"]}, {"argv": ["/usr/bin/python", "-c", "pass"]},
    {"argv": "python -c pass"}, {"argv": ["python"], "env": {"KEY": "secret"}},
    {"argv": ["python"], "cwd": "input"}, {"argv": ["python"], "mount": "/"},
])
def test_commands_reject_shell_and_privilege_parameters(arguments):
    with pytest.raises(ValueError):
        validate_operation("run_command", arguments)


def test_readonly_input_and_fixed_argv_contract():
    validate_operation("read_file", {"path": "input/request.json"})
    validate_operation("run_command", {"argv": ["python", "-c", "print(1)"]})
    with pytest.raises(ValueError):
        validate_operation("write_file", {"path": "input/request.json", "content": "oops"})
    with pytest.raises(ValueError):
        validate_operation("read_file", {"path": "input/request.json", "follow_links": True})


def test_workspace_is_unique_only_contains_payload_and_scratch(tmp_path):
    first = create_workspace(tmp_path, {"text": "审查上下文", "file": "main.docx"})
    second = create_workspace(tmp_path, {"text": "different"})
    assert first != second
    assert {path.name for path in first.iterdir()} == {"input", "scratch"}
    assert json.loads((first / "input/request.json").read_text(encoding="utf-8"))["text"] == "审查上下文"
    assert not list((first / "scratch").iterdir())


def test_bwrap_command_has_fixed_boundaries(monkeypatch, tmp_path):
    import app.sandbox as module
    monkeypatch.setattr(module, "_linux_available", lambda _: Path("/usr/bin/bwrap"))
    monkeypatch.setattr(module.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(module.sys, "prefix", "/app/.venv")
    command = build_command(Settings(), tmp_path)
    for flag in ("--unshare-user", "--unshare-pid", "--unshare-net", "--unshare-ipc", "--unshare-uts",
                 "--clearenv", "--as-pid-1", "--die-with-parent", "--new-session"):
        assert flag in command
    assert "--disable-userns" not in command  # Docker correctly keeps /proc/sys read-only.
    assert command.count("--bind") == 1
    assert command[command.index("--bind") + 1:command.index("--bind") + 3] == [str(tmp_path / "scratch"), "/workspace/scratch"]
    assert "--cap-drop" in command and "ALL" in command
    assert not any(value in {"/app", "/home", "/root", "/etc", "/data"} for value in command)
    assert "LLM_API_KEY" not in " ".join(command)


@pytest.mark.skipif(sys.platform == "linux", reason="非 Linux 必须拒绝执行")
def test_non_linux_never_falls_back_to_host_execution(tmp_path):
    sandbox = WorkspaceSandbox(Settings(), create_workspace(tmp_path, {}))
    with pytest.raises(SandboxUnavailable, match="Linux"):
        asyncio.run(sandbox.execute("run_command", {"argv": ["python", "-c", "print('unsafe')"]}))
    asyncio.run(sandbox.close())


def test_only_four_mcp_tools_are_registered(tmp_path):
    from app.sandbox_mcp import create_server
    server = create_server(WorkspaceSandbox(Settings(), create_workspace(tmp_path, {})))
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {"list_files", "read_file", "write_file", "run_command"}


LINUX = pytest.mark.skipif(sys.platform != "linux", reason="需要真实 Linux bubblewrap namespace，Windows 不模拟隔离通过")


@LINUX
def test_real_linux_preflight():
    asyncio.run(preflight(Settings(_env_file=None)))


@LINUX
def test_real_linux_files_symlinks_commands_and_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "DO-NOT-LEAK")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "private-codex"))
    workspace = create_workspace(tmp_path, {"facts": ["hello"]})
    sandbox = WorkspaceSandbox(Settings(_env_file=None), workspace)

    async def run():
        try:
            await sandbox.execute("write_file", {"path": "scratch/nested/check.py", "content": "print(6 * 7)"})
            result = await sandbox.execute("run_command", {"argv": ["python", "nested/check.py"]})
            assert result["exit_code"] == 0 and result["stdout"].strip() == "42"
            result = await sandbox.execute("read_file", {"path": "input/request.json"})
            assert "hello" in result["content"]
            for target in ["/proc/1/environ", str(tmp_path / "secret"), "/workspace/input/request.json"]:
                await sandbox.execute("run_command", {"argv": ["python", "-c", f"import os; os.symlink({target!r},'link')"]})
                with pytest.raises(SandboxError):
                    await sandbox.execute("read_file", {"path": "scratch/link"})
                with pytest.raises(SandboxError):
                    await sandbox.execute("write_file", {"path": "scratch/link", "content": "bad"})
                await sandbox.execute("run_command", {"argv": ["python", "-c", "import os; os.unlink('link')"]})
            result = await sandbox.execute("run_command", {"argv": ["python", "-c", "import os; print(dict(os.environ))"]})
            assert "DO-NOT-LEAK" not in result["stdout"] and "CODEX_HOME" not in result["stdout"]
        finally:
            await sandbox.close()
    asyncio.run(run())
    assert not workspace.exists()


@LINUX
def test_real_linux_timeout_and_cancel_kill_detached_processes(tmp_path):
    async def run():
        for cancel in (False, True):
            workspace = create_workspace(tmp_path, {})
            sandbox = WorkspaceSandbox(Settings(_env_file=None, sandbox_command_timeout=1), workspace)
            child = "import time,pathlib; time.sleep(1.5); pathlib.Path('/workspace/scratch/leaked').write_text('escaped')"
            script = f"import subprocess,time; subprocess.Popen(['python','-c',{child!r}],start_new_session=True); time.sleep(30)"
            try:
                task = asyncio.create_task(sandbox.execute("run_command", {"argv": ["python", "-c", script]}))
                if cancel:
                    await asyncio.sleep(0.2)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises(SandboxError, match="超时"):
                        await task
                await asyncio.sleep(1.6)
                assert not (workspace / "scratch/leaked").exists()
            finally:
                await sandbox.close()
    asyncio.run(run())


@LINUX
def test_real_linux_output_limit_and_file_limit(tmp_path):
    workspace = create_workspace(tmp_path, {})
    sandbox = WorkspaceSandbox(Settings(_env_file=None, sandbox_output_bytes=1024, sandbox_file_mb=1), workspace)
    async def run():
        try:
            with pytest.raises(SandboxError, match="输出"):
                await sandbox.execute("run_command", {"argv": ["python", "-c", "print('x'*100000)"]})
            result = await sandbox.execute("run_command", {"argv": ["python", "-c", "open('large','wb').write(b'x'*2000000)"]})
            assert result["exit_code"] != 0
            assert (workspace / "scratch/large").stat().st_size <= 1024 * 1024
        finally:
            await sandbox.close()
    asyncio.run(run())


@LINUX
def test_real_linux_filter_blocks_nested_namespaces_and_parent_memory(tmp_path):
    workspace = create_workspace(tmp_path, {})
    sandbox = WorkspaceSandbox(Settings(_env_file=None), workspace)
    script = "\n".join([
        "import ctypes,errno,os,subprocess",
        "libc=ctypes.CDLL(None,use_errno=True)",
        "assert libc.unshare(0x10000000)==-1 and ctypes.get_errno()==errno.EPERM",
        "assert libc.ptrace(0,0,0,0)==-1 and ctypes.get_errno()==errno.EPERM",
        "try:",
        " open('/proc/1/mem','rb')",
        " raise AssertionError('parent memory accessible')",
        "except PermissionError: pass",
        "assert subprocess.run(['python','-c','print(42)'],capture_output=True,text=True).stdout.strip()=='42'",
        "print('restricted')",
    ])

    async def run():
        try:
            result = await sandbox.execute("run_command", {"argv": ["python", "-c", script]})
            assert result["exit_code"] == 0, result
            assert "restricted" in result["stdout"]
        finally:
            await sandbox.close()
    asyncio.run(run())


@LINUX
def test_real_linux_deleted_open_files_count_toward_workspace_quota(tmp_path):
    workspace = create_workspace(tmp_path, {})
    sandbox = WorkspaceSandbox(Settings(_env_file=None, sandbox_workspace_mb=16), workspace)
    script = "\n".join([
        "import os,time",
        "file=open('deleted','w+b')",
        "os.unlink('deleted')",
        "file.write(b'x'*(20*1024*1024)); file.flush()",
        "time.sleep(5)",
    ])

    async def run():
        try:
            with pytest.raises(SandboxError, match="容量"):
                await sandbox.execute("run_command", {"argv": ["python", "-c", script]})
        finally:
            await sandbox.close()
    asyncio.run(run())


@LINUX
def test_real_linux_child_cannot_disable_workspace_observation(tmp_path):
    workspace = create_workspace(tmp_path, {})
    sandbox = WorkspaceSandbox(Settings(_env_file=None), workspace)
    script = "import ctypes,time; libc=ctypes.CDLL(None); assert libc.prctl(4,0,0,0,0)==0; time.sleep(5)"

    async def run():
        try:
            with pytest.raises(SandboxError, match="核验"):
                await sandbox.execute("run_command", {"argv": ["python", "-c", script]})
        finally:
            await sandbox.close()
    asyncio.run(run())


@LINUX
def test_cleanup_restores_directory_modes_without_following_symlinks(tmp_path):
    workspace = create_workspace(tmp_path / "workspaces", {})
    outside = tmp_path / "keep"
    outside.mkdir()
    (outside / "secret.txt").write_text("must remain", encoding="utf-8")
    nested = workspace / "scratch" / "nested"
    nested.mkdir()
    (nested / "text.txt").write_text("discard", encoding="utf-8")
    (nested / "link").symlink_to(outside, target_is_directory=True)
    nested.chmod(0)
    (workspace / "scratch").chmod(0)
    cleanup_workspace(workspace)
    assert not workspace.exists()
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "must remain"
