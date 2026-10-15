"""Start the pinned runtime in a private process group, including its MCP child.

This launcher is application-owned and never receives model-selected arguments.
The adapter kills the group after SDK shutdown so stdio MCP children cannot leak.
"""
import os
import sys


if __name__ == "__main__":
    if sys.platform != "linux":
        raise SystemExit("Codex production launcher requires Linux")
    os.setsid()
    os.execv(sys.argv[1], sys.argv[1:])
