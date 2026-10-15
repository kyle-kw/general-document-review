"""Local stdio MCP gateway. It exposes no host filesystem or shell tools."""

import argparse
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from app.config import Settings
from app.sandbox import WorkspaceSandbox


def create_server(sandbox: WorkspaceSandbox) -> MCPServer:
    server = MCPServer("review-workspace", instructions=(
        "仅可读取 input/ 和 scratch/；仅可写入 scratch/。执行参数必须为数组，"
        "可用命令为 python、python3、officecli、ls、cat、head、tail、wc、rg。所有执行均在隔离环境中。"
        "OfficeCLI 使用 input/skills/officecli/SKILL.md 中的审查技能；须直接调用，不启动服务或安装插件。"
    ))

    @server.tool()
    async def list_files(path: str = "input") -> dict:
        """列出工作区相对目录，禁止跟随符号链接。"""
        return await sandbox.execute("list_files", {"path": path})

    @server.tool()
    async def read_file(path: str, offset: int = 0, limit: int = 65536) -> dict:
        """读取 UTF-8 文本片段；offset 和 limit 单位为字节。"""
        return await sandbox.execute("read_file", {"path": path, "offset": offset, "limit": limit})

    @server.tool()
    async def write_file(path: str, content: str) -> dict:
        """仅在 scratch/ 中写入 UTF-8 文件，可创建父目录。"""
        return await sandbox.execute("write_file", {"path": path, "content": content})

    @server.tool()
    async def run_command(argv: list[str], cwd: str = "scratch") -> dict:
        """执行允许的命令数组；无宿主 shell、网络、自定义环境或挂载。"""
        return await sandbox.execute("run_command", {"argv": argv, "cwd": cwd})

    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    # Adapter supplies only sandbox settings. Never load .env in this process.
    sandbox = WorkspaceSandbox(Settings(_env_file=None), args.workspace)
    create_server(sandbox).run(transport="stdio")


if __name__ == "__main__":
    main()
