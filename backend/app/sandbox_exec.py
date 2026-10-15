"""Trusted PID 1 for one sandbox operation (executed directly, never imported by agents).

This file deliberately uses only the standard library. All agent supplied paths are
opened *inside* the namespace through non-following directory descriptors.
"""

import ctypes
import errno
import json
import os
from pathlib import PurePosixPath
import resource
import signal
import stat
import subprocess
import sys
import time
from typing import Any


ROOT = "/workspace"
COMMANDS = {"python", "python3", "ls", "cat", "head", "tail", "wc", "rg", "officecli"}
OFFICECLI_COMMANDS = {
    "help", "--help", "-h", "--version", "--output-schema-crc", "get", "query", "view",
    "validate", "raw", "dump", "create", "set", "add", "remove", "move", "swap", "batch",
    "raw-set", "add-part",
}
OFFICECLI_ENV = {
    "OFFICECLI_NO_AUTO_INSTALL": "1", "OFFICECLI_NO_AUTO_RESIDENT": "1", "OFFICECLI_SKIP_UPDATE": "1",
    "DOTNET_EnableDiagnostics": "0", "DOTNET_GCHeapHardLimit": "10000000", "DOTNET_PROCESSOR_COUNT": "2",
    "DOTNET_BUNDLE_EXTRACT_BASE_DIR": "/workspace/scratch/.net",
}
LIMIT_EXIT_CODE = 125
LIMIT_MARKER = "REVIEW_SANDBOX_LIMIT:"


class QuotaInspectionError(RuntimeError):
    """A workspace that cannot be measured is not allowed to keep executing."""


class ProcessInspectionError(QuotaInspectionError):
    """A transient exec/exit transition may briefly hide a /proc process view."""


def install_agent_filter() -> None:
    """Apply an inherited seccomp boundary after bubblewrap finishes setup.

    bwrap --disable-userns writes a userns sysctl, which is correctly read-only
    under Docker. An unprivileged, inherited syscall filter needs no such write.
    Keep PID 1 nondumpable so a same-UID child cannot open /proc/1/mem either.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    for option, value in ((38, 1), (4, 0)):  # PR_SET_NO_NEW_PRIVS, PR_SET_DUMPABLE
        if libc.prctl(option, value, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "无法安装沙箱进程保护")

    class Comparison(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_uint),
                    ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]

    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                             ctypes.c_uint, ctypes.POINTER(Comparison)]
    seccomp.seccomp_rule_add_array.restype = ctypes.c_int
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_load.restype = ctypes.c_int
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    allow = 0x7FFF0000
    deny = 0x00050000 | errno.EPERM
    context = seccomp.seccomp_init(allow)
    if not context:
        raise OSError("无法创建沙箱系统调用过滤器")

    def rule(name: str, action: int, comparisons: Any = None) -> None:
        syscall = seccomp.seccomp_syscall_resolve_name(name.encode("ascii"))
        if syscall < 0:
            raise OSError(f"无法解析必需的系统调用保护：{name}")
        result = seccomp.seccomp_rule_add_array(context, action, syscall, 1 if comparisons else 0, comparisons)
        if result < 0:
            raise OSError(-result, f"无法设置系统调用保护：{name}")

    try:
        for name in ("unshare", "setns", "mount", "pivot_root", "umount2", "ptrace",
                     "process_vm_readv", "process_vm_writev", "pidfd_getfd", "process_madvise"):
            rule(name, deny)
        # Return ENOSYS so libc can fall back to ordinary clone() for threads and
        # subprocesses; seccomp cannot inspect the pointed-to clone3 struct.
        rule("clone3", 0x00050000 | errno.ENOSYS)
        # SCMP_CMP_MASKED_EQ=7. Ordinary forks/threads remain permitted; all clone
        # namespace bits are denied even on kernels allowing userns nesting.
        for namespace_bit in (0x00020000, 0x02000000, 0x04000000, 0x08000000,
                              0x10000000, 0x20000000, 0x40000000):
            comparison = Comparison(0, 7, namespace_bit, namespace_bit)
            rule("clone", deny, ctypes.pointer(comparison))
        result = seccomp.seccomp_load(context)
        if result < 0:
            raise OSError(-result, "无法启用系统调用保护")
    finally:
        seccomp.seccomp_release(context)


def scratch_usage(scratch_root: str = "/workspace/scratch", proc_root: str = "/proc") -> int:
    """Measure linked and deleted-but-open/mapped files inside this PID namespace.

    This is an additional sampled workspace guard, not a kernel filesystem quota.
    Per-file rlimits and the worker container's aggregate caps remain necessary.
    No path observed here is ever resolved or opened by the host application.
    """
    inodes: dict[tuple[int, int], int] = {}

    def add(info: os.stat_result) -> None:
        if stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode):
            allocated = getattr(info, "st_blocks", 0) * 512
            identity = (info.st_dev, info.st_ino)
            inodes[identity] = max(inodes.get(identity, 0), info.st_size, allocated)

    def visit(descriptor: int, depth: int = 0) -> None:
        if depth > 128:
            raise QuotaInspectionError("工作区目录深度超过限制，进程已停止")
        add(os.fstat(descriptor))
        with os.scandir(descriptor) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                        try:
                            visit(child, depth + 1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode):
                        add(info)
                except (FileNotFoundError, NotADirectoryError):
                    continue

    prefix = scratch_root.rstrip("/") + "/"
    try:
        root = os.open(scratch_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            visit(root)
        finally:
            os.close(root)
        for pid in os.listdir(proc_root):
            if not pid.isdecimal() or int(pid) == os.getpid():
                continue
            process_root = proc_root + "/" + pid
            try:
                # A file unlinked after opening continues occupying disk space.
                # Namespace-local /proc exposes its inode through the open fd.
                for name in os.listdir(process_root + "/fd"):
                    source = process_root + "/fd/" + name
                    try:
                        if os.readlink(source).startswith(prefix):
                            add(os.stat(source))
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                # mmap() can retain a deleted file after the final fd is closed.
                with open(process_root + "/maps", encoding="utf-8", errors="replace") as mappings:
                    for line in mappings:
                        fields = line.rstrip("\n").split(None, 5)
                        if len(fields) == 6 and fields[5].startswith(prefix):
                            identity = int(fields[4])
                            device = os.makedev(*(int(part, 16) for part in fields[3].split(":")))
                            if (device, identity) not in inodes:
                                try:
                                    add(os.stat(process_root + "/map_files/" + fields[0]))
                                except (FileNotFoundError, ProcessLookupError):
                                    continue
            except (FileNotFoundError, ProcessLookupError):
                continue  # Normal exit while the snapshot is being collected.
            except PermissionError as exc:
                # Linux makes a zombie's fd directory inaccessible even when
                # the process still appears in the namespace's /proc listing.
                try:
                    with open(process_root + "/stat", encoding="utf-8") as status:
                        state = status.read().rsplit(")", 1)[1].strip().split()[0]
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if state in {"Z", "X", "x"}:
                    continue
                raise ProcessInspectionError("无法核验活动进程的工作区容量，进程已停止") from exc
    except OSError as exc:
        # prctl(PR_SET_DUMPABLE,0), chmod or similar tricks must not turn off
        # quota enforcement by making a live process/file unobservable.
        raise QuotaInspectionError(f"无法核验工作区容量（errno={exc.errno}, path={exc.filename}），进程已停止") from exc
    return sum(inodes.values())


def _run_command(executable: str, argv: list[str], limits: dict) -> int:
    def child_setup() -> None:
        # PID 1 remains nondumpable. Only the soon-to-exec child must be
        # observable by its trusted parent; the helper is single-threaded here.
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        libc.prctl.restype = ctypes.c_int
        if libc.prctl(4, 1, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
            raise OSError(ctypes.get_errno(), "无法初始化子进程观测权限")

    process = subprocess.Popen([executable, *argv[1:]], stdin=subprocess.DEVNULL,
                               close_fds=True, preexec_fn=child_setup)
    unobservable_since: float | None = None
    try:
        while True:
            status = process.poll()
            if status is not None:
                return status
            try:
                usage = scratch_usage()
            except ProcessInspectionError:
                # exec credential changes can temporarily reset dumpability.
                # Never disable accounting indefinitely for a hostile process.
                unobservable_since = unobservable_since or time.monotonic()
                if time.monotonic() - unobservable_since >= 0.15:
                    raise
                time.sleep(0.01)
                continue
            unobservable_since = None
            if usage > limits["workspace"]:
                raise QuotaInspectionError("工作区容量超过限制（含已删除但仍打开的文件），进程已停止")
            time.sleep(0.02)
    except QuotaInspectionError as exc:
        # This helper is PID 1 only in its isolated PID namespace. Never send a
        # namespace-wide signal if the helper was accidentally launched directly.
        if os.getpid() == 1:
            os.kill(-1, signal.SIGKILL)
        else:
            process.kill()
        process.wait()
        print(LIMIT_MARKER + str(exc), file=sys.stderr, flush=True)
        return LIMIT_EXIT_CODE


def _parts(value: str, writable: bool = False) -> tuple[str, ...]:
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise ValueError("路径必须为工作区中的相对路径")
    parts = PurePosixPath(value).parts
    if not parts or value.startswith("/") or any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("禁止绝对路径与路径穿越")
    if parts[0] not in ({"scratch"} if writable else {"input", "scratch"}):
        raise ValueError("路径只能位于 input/ 或 scratch/；写入只允许 scratch/")
    return parts


def _directory(parts: tuple[str, ...], create: bool = False) -> int:
    descriptor = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _file_operation(operation: str, arguments: dict, output_limit: int) -> dict:
    parts = _parts(arguments.get("path", "input"), writable=operation == "write_file")
    if operation == "list_files":
        directory = _directory(parts)
        try:
            entries = []
            # Return one directory, never recursively follow attacker-created links.
            for name in sorted(os.listdir(directory)):
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                import stat
                kind = "symlink" if stat.S_ISLNK(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else "file"
                entries.append({"name": name, "type": kind, "size": info.st_size})
                if len(json.dumps(entries, ensure_ascii=False).encode()) > output_limit // 2:
                    return {"entries": entries[:-1], "truncated": True}
            return {"entries": entries, "truncated": False}
        finally:
            os.close(directory)
    if len(parts) < 2:
        raise ValueError("请指定文件路径")
    directory = _directory(parts[:-1], create=operation == "write_file")
    try:
        if operation == "read_file":
            offset = arguments.get("offset", 0)
            limit = arguments.get("limit", 65536)
            if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
                raise ValueError("offset 必须非负，limit 必须为正整数")
            limit = min(limit, output_limit // 4)
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                import stat
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("只能读取普通文件")
                os.lseek(descriptor, offset, os.SEEK_SET)
                data = os.read(descriptor, limit)
                return {"content": data.decode("utf-8", errors="replace"), "offset": offset,
                        "size": info.st_size, "truncated": offset + len(data) < info.st_size}
            finally:
                os.close(descriptor)
        if operation == "write_file":
            content = arguments.get("content")
            if not isinstance(content, str):
                raise ValueError("content 必须是字符串")
            data = content.encode("utf-8")
            if len(data) > output_limit:
                raise ValueError("单次写入超过限制")
            descriptor = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 0o600, dir_fd=directory)
            try:
                import stat
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("只能写入普通文件")
                view = memoryview(data)
                while view:
                    view = view[os.write(descriptor, view):]
                return {"path": "/".join(parts), "bytes_written": len(data)}
            finally:
                os.close(descriptor)
        raise ValueError("不支持的操作")
    finally:
        os.close(directory)


def main() -> int:
    if os.getpid() != 1:
        raise SystemExit("Trusted helper requires isolated PID 1")
    install_agent_filter()
    request = json.loads(sys.stdin.buffer.readline(8 * 1024 * 1024))
    limits = request["limits"]
    operation = request["operation"]
    arguments = request["arguments"]
    command_argv = arguments.get("argv")
    direct_officecli = operation == "run_command" and isinstance(command_argv, list) and bool(command_argv) and command_argv[0] == "officecli"
    address_space = limits["officecli_as"] if direct_officecli else limits["memory"]
    resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
    resource.setrlimit(resource.RLIMIT_NPROC, (limits["pids"], limits["pids"]))
    resource.setrlimit(resource.RLIMIT_FSIZE, (limits["file"], limits["file"]))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_CPU, (limits["seconds"], limits["seconds"] + 1))
    try:
        if operation == "run_command":
            argv = arguments["argv"]
            if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and "\x00" not in arg for arg in argv):
                raise ValueError("argv 必须为字符串参数数组")
            if argv[0] not in COMMANDS:
                raise ValueError("命令不在允许列表中")
            if argv[0] == "officecli":
                if len(argv) < 2 or argv[1] not in OFFICECLI_COMMANDS or "--browser" in argv:
                    raise ValueError("OfficeCLI 命令不在审查允许列表中")
                if argv[1] == "view" and (len(argv) < 4 or argv[3] not in {"text", "outline", "stats", "annotated", "issues"}):
                    raise ValueError("OfficeCLI view 模式不在允许列表中")
                os.environ.update(OFFICECLI_ENV)
            cwd = _parts(arguments.get("cwd", "scratch"), writable=True)
            directory = _directory(cwd)
            try:
                os.fchdir(directory)
            finally:
                os.close(directory)
            executable = ("/runtime/venv/bin/python" if argv[0] in {"python", "python3"}
                          else "/usr/local/bin/officecli" if argv[0] == "officecli" else "/usr/bin/" + argv[0])
            # PID 1 exits immediately after this child: the kernel kills every
            # remaining descendant, including processes that called setsid().
            return _run_command(executable, argv, limits)
        result = _file_operation(operation, arguments, limits["output"])
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
