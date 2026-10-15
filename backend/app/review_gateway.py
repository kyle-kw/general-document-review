"""Loopback-only provider gate: the model sees exactly the four workspace tools.

The pinned Codex runtime also advertises generic MCP resource helpers. They have
no application resources, but must still be removed from the model tool surface.
This gate never exposes an API outside the worker and never follows redirects.
"""
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import socket
from threading import Lock, Thread
from typing import Any

import httpx

from .config import Settings


TOOLS = frozenset({"list_files", "read_file", "write_file", "run_command"})
NAMESPACE = "mcp__review_workspace"


def restricted_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for tool in tools:
        if tool.get("type") == "namespace" and tool.get("name") == NAMESPACE:
            selected = [item for item in tool["tools"] if item.get("type") == "function" and item.get("name") in TOOLS]
            result.append({**tool, "tools": selected})
    if len(result) != 1 or {item["name"] for item in result[0]["tools"]} != TOOLS:
        raise ValueError("The runtime did not supply the four required workspace tools")
    return result


def _normalize_call(item: dict[str, Any]) -> None:
    name = item.get("name", "")
    namespace = item.get("namespace")
    for prefix in (NAMESPACE + ".", NAMESPACE + "__"):
        if name.startswith(prefix):
            name, namespace = name[len(prefix):], NAMESPACE
            break
    if namespace != NAMESPACE or name not in TOOLS:
        raise ValueError("Unapproved model tool call")
    item.update(name=name, namespace=namespace)


class ReviewGateway(AbstractContextManager):
    def __init__(self, settings: Settings, mode: str = "review"):
        if mode not in {"review", "requirements"}:
            raise ValueError("Unknown execution mode")
        self.mode = mode
        self.token = secrets.token_urlsafe(32)
        token = self.token
        gateway = self
        self._lock = Lock()
        self._stopped = False
        self._sockets: set[Any] = set()
        self.client = httpx.Client(timeout=httpx.Timeout(settings.llm_timeout, connect=min(settings.llm_timeout, 10)),
                                   trust_env=False)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path != "/v1/responses" or self.headers.get("Authorization") != f"Bearer {token}":
                    self.send_error(404)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if size <= 0 or size > 16 * 1024 * 1024:
                        raise ValueError("Invalid provider request size")
                    body = json.loads(self.rfile.read(size))
                    body["tools"] = restricted_tools(body.get("tools", [])) if mode == "review" else []
                    if mode == "requirements":
                        body.pop("tool_choice", None)
                    with gateway.client.stream("POST", settings.llm_base_url.rstrip("/") + "/responses",
                                               headers={"Authorization": "Bearer " + settings.llm_api_key.get_secret_value()},
                                               json=body, extensions={"trace": gateway._trace}) as response:
                        self.send_response(response.status_code)
                        self.send_header("Content-Type", response.headers.get("content-type", "application/json"))
                        self.end_headers()
                        # Validate names before passing SSE items to the runtime.
                        for line in response.iter_lines():
                            if line.startswith("data:") and line[5:].strip() != "[DONE]":
                                event = json.loads(line[5:])
                                if mode == "requirements" and any(part in event.get("type", "") for part in ("function_call", "tool_call")):
                                    raise ValueError("Tools are forbidden while rewriting requirements")
                                item = event.get("item", {})
                                if mode == "requirements" and item and item.get("type") not in {"message", "reasoning"}:
                                    raise ValueError("Tools are forbidden while rewriting requirements")
                                if item.get("type") == "function_call":
                                    _normalize_call(item)
                                for output in event.get("response", {}).get("output", []):
                                    if mode == "requirements" and output.get("type") not in {"message", "reasoning"}:
                                        raise ValueError("Tools are forbidden while rewriting requirements")
                                    if output.get("type") == "function_call":
                                        _normalize_call(output)
                                line = "data: " + json.dumps(event, ensure_ascii=False)
                            self.wfile.write((line + "\n").encode())
                            self.wfile.flush()
                except (ValueError, OSError, RuntimeError, httpx.HTTPError):
                    # No provider error, request text or credentials enter logs.
                    self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    @staticmethod
    def _disconnect(connection: Any) -> None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _trace(self, name: str, info: dict[str, Any]) -> None:
        # Capture the socket as soon as TCP/TLS connects, including requests that
        # stall before response headers. Closing a socket alone on another thread
        # does not reliably wake a blocking recv; shutdown does.
        stream = info.get("return_value")
        if not name.endswith(".complete") or not hasattr(stream, "get_extra_info"):
            return
        connection = stream.get_extra_info("socket")
        if connection is not None:
            with self._lock:
                stopped = self._stopped
                if not stopped:
                    self._sockets.add(connection)
            if stopped:
                self._disconnect(connection)
                raise OSError("Provider gate closed")

    def __exit__(self, *args):
        with self._lock:
            self._stopped = True
            connections, self._sockets = self._sockets, set()
        for connection in connections:
            self._disconnect(connection)
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
