"""Local gateway tests exercise the real pinned Codex runtime, without an API key."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
import sys
import textwrap

import pytest
from jsonschema import ValidationError

from app.codex_client import AgentError, CodexAgentClient, ReviewCancelled, parse_agent_output
from app.config import Settings

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}


@pytest.fixture
def gateway():
    requests = []
    control = {"text": '{"ok":true}', "calls": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            requests.append((self.path, dict(self.headers), payload))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            index = len(requests) - 1
            if "forced_item" in control:
                item = control["forced_item"]
            elif index < len(control["calls"]):
                tool, arguments = control["calls"][index]
                matches = [t["name"] for t in payload["tools"] if t["type"] == "function" and t["name"].endswith("__" + tool)]
                matches += [namespace["name"] + "." + t["name"] for namespace in payload["tools"]
                            if namespace["type"] == "namespace" for t in namespace["tools"] if t["name"] == tool]
                assert matches, json.dumps(payload["tools"], indent=2)
                name = matches[0]
                item = {"id": f"fc_{index}", "type": "function_call", "status": "completed",
                        "call_id": f"call_{index}", "name": name, "arguments": json.dumps(arguments)}
            else:
                item = {"id": "msg_test", "type": "message", "status": "completed", "role": "assistant",
                        "content": [{"type": "output_text", "text": control["text"], "annotations": []}]}
            response = {"id": "resp_test", "object": "response", "created_at": 1,
                        "status": "completed", "model": "local-review-test", "output": [item],
                        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                                  "input_tokens_details": {"cached_tokens": 0},
                                  "output_tokens_details": {"reasoning_tokens": 0}}}
            for event in [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1", requests, control
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
def mock_sandbox_mcp(monkeypatch, tmp_path):
    """A subprocess stub validates actual SDK/MCP transport, not Linux isolation."""
    from app import codex_client

    async def mocked_preflight(settings):
        pass

    monkeypatch.setattr(codex_client, "preflight", mocked_preflight)
    script = tmp_path / "mcp_stub.py"
    log = tmp_path / "tool-calls.jsonl"
    script.write_text(textwrap.dedent('''
        import json
        import sys
        from pathlib import Path
        from mcp.server import MCPServer

        server = MCPServer("review_workspace")
        root = Path(sys.argv[1])
        log = Path(sys.argv[2])
        def record(name, args):
            with log.open("a", encoding="utf-8") as file:
                file.write(json.dumps({"name": name, "args": args, "workspace": str(root)}) + "\\n")
            return {"ok": True, "marker": "tool-reached-subprocess"}
        @server.tool()
        def list_files(path: str = "input") -> dict:
            return record("list_files", {"path": path})
        @server.tool()
        def read_file(path: str, offset: int = 0, limit: int = 1000) -> dict:
            return record("read_file", {"path": path})
        @server.tool()
        def write_file(path: str, content: str) -> dict:
            return record("write_file", {"path": path, "content": content})
        @server.tool()
        def run_command(argv: list[str], cwd: str = "scratch") -> dict:
            return record("run_command", {"argv": argv, "cwd": cwd})
        server.run(transport="stdio")
    '''), encoding="utf-8")
    original = CodexAgentClient._runtime

    def runtime(self, home, workspace, gateway):
        codex = original(self, home, workspace, gateway)
        if gateway.mode == "requirements":
            return codex
        from dataclasses import replace
        config = codex._client._sync.config
        # Replace only the application MCP executable with a test stub; keep all
        # pinned runtime restrictions and policy unchanged.
        server = {"command": sys.executable, "args": [str(script), str(workspace), str(log)],
                  "cwd": str(tmp_path), "env_vars": [], "required": True,
                  "default_tools_approval_mode": "approve",
                  "enabled_tools": ["list_files", "read_file", "write_file", "run_command"]}
        overrides = tuple(value for value in config.config_overrides if not value.startswith("mcp_servers="))
        overrides += ("mcp_servers={review_workspace=" + codex_client._toml_value(server) + "}",)
        launch = config.launch_args_override
        if launch is not None:
            launch = (sys.executable, str(Path(codex_client.__file__).with_name("codex_runtime_launcher.py")),
                      str(codex_client.bundled_codex_path()),
                      *(item for value in overrides for item in ("--config", value)),
                      "app-server", "--listen", "stdio://")
        codex._client._sync.config = replace(config, config_overrides=overrides, launch_args_override=launch)
        return codex

    monkeypatch.setattr(CodexAgentClient, "_runtime", runtime)
    return log


async def test_real_sdk_gateway_only_exposes_four_workspace_tools(gateway, tmp_path, mock_sandbox_mcp):
    url, requests, control = gateway
    settings = Settings(_env_file=None, llm_base_url=url, llm_api_key="test-local-only",
                        llm_model="local-review-test", llm_timeout=20, llm_max_retries=0)
    control["calls"] = [("list_files", {}), ("read_file", {"path": "input/request.json"}),
                        ("write_file", {"path": "scratch/check.py", "content": "print(2 + 2)"}),
                        ("run_command", {"argv": ["python", "scratch/check.py"]})]
    async with CodexAgentClient(settings, tmp_path / "runtime", lambda: False) as client:
        result = await client.run_agent("[test] Return ok=true", {"text": "test"}, SCHEMA)
    assert result == {"ok": True}
    assert requests
    path, headers, body = requests[-1]
    assert path == "/v1/responses"
    assert {k.lower(): v for k, v in headers.items()}["authorization"] == "Bearer test-local-only"
    assert body["model"] == "local-review-test"
    assert len(body["tools"]) == 1
    namespace = body["tools"][0]
    assert namespace["type"] == "namespace" and namespace["name"] == "mcp__review_workspace"
    assert {tool["name"] for tool in namespace["tools"]} == {"list_files", "read_file", "write_file", "run_command"}
    assert body["text"]["format"]["schema"] == SCHEMA
    assert "tool-reached-subprocess" in json.dumps(body["input"])
    calls = [json.loads(line) for line in mock_sandbox_mcp.read_text().splitlines()]
    assert [call["name"] for call in calls] == [name for name, _ in control["calls"]]
    assert not list((tmp_path / "runtime" / "workspaces").iterdir())
    assert not list((tmp_path / "runtime" / "runtime-homes").iterdir())
    assert next((tmp_path / "runtime" / "diagnostics").glob("*/raw.txt")).read_text() == '{"ok":true}'


async def test_requirements_mode_has_no_tools_source_workspace_or_skill(gateway, tmp_path, mock_sandbox_mcp):
    url, requests, control = gateway
    settings = Settings(_env_file=None, llm_base_url=url, llm_api_key="test-local-only",
                        llm_model="local-review-test", llm_timeout=20, llm_max_retries=0)
    source = tmp_path / "private.docx"
    source.write_bytes(b"PRIVATE SOURCE MUST NOT REACH REWRITER")
    async with CodexAgentClient(settings, tmp_path / "runtime", lambda: False, source_files={"main": source}) as client:
        assert await client.run_agent("[test] Compile only", {"raw": "用户原始要求", "document_id": "main"},
                                      SCHEMA, mode="requirements") == {"ok": True}
    body = requests[-1][2]
    assert body["tools"] == []
    assert "PRIVATE SOURCE" not in json.dumps(body)
    assert "input/request.json" not in json.dumps(body)
    assert not mock_sandbox_mcp.exists()
    assert not (tmp_path / "runtime/workspaces").exists()


async def test_requirements_gateway_rejects_even_model_invented_tool_calls(gateway, tmp_path, mock_sandbox_mcp):
    url, requests, control = gateway
    control["forced_item"] = {"id": "bad-call", "type": "function_call", "call_id": "bad-call",
                              "name": "mcp__review_workspace.run_command", "arguments": '{"argv":["python"]}'}
    settings = Settings(_env_file=None, llm_base_url=url, llm_api_key="test-local-only",
                        llm_model="local-review-test", llm_timeout=5, llm_max_retries=0)
    async with CodexAgentClient(settings, tmp_path / "runtime", lambda: False) as client:
        with pytest.raises(AgentError):
            await client.run_agent("[test] Compile only", {"raw": "example"}, SCHEMA, mode="requirements")
    assert requests and requests[-1][2]["tools"] == []
    assert not mock_sandbox_mcp.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="需要真实 Linux 沙箱及打包运行时")
async def test_real_sdk_mcp_executes_python_in_linux_sandbox(gateway, tmp_path):
    url, requests, control = gateway
    settings = Settings(_env_file=None, llm_base_url=url, llm_api_key="test-local-only",
                        llm_model="local-review-test", llm_timeout=30, llm_max_retries=0)
    control["calls"] = [("read_file", {"path": "input/request.json"}),
                        ("write_file", {"path": "scratch/check.py", "content": "print(6 * 7)"}),
                        ("run_command", {"argv": ["python", "check.py"]})]
    async with CodexAgentClient(settings, tmp_path / "runtime", lambda: False) as client:
        assert await client.run_agent("[test] Return ok=true", {"text": "test"}, SCHEMA) == {"ok": True}
    content = json.dumps(requests[-1][2]["input"])
    assert '42' in content
    assert "unsupported call" not in content and "Error executing tool" not in content
    assert not list((tmp_path / "runtime/workspaces").iterdir())


def test_provider_gate_rejects_unapproved_tools_and_names():
    from app.review_gateway import _normalize_call, restricted_tools
    with pytest.raises(ValueError):
        restricted_tools([{"type": "function", "name": "run_command"}])
    for item in [{"name": "exec_command"}, {"name": "read_file", "namespace": "other"},
                 {"name": "list_mcp_resources"}]:
        with pytest.raises(ValueError):
            _normalize_call(item)


async def test_configuration_fails_before_runtime(tmp_path):
    settings = Settings(_env_file=None)
    with pytest.raises(AgentError, match="LLM_BASE_URL"):
        async with CodexAgentClient(settings, tmp_path, lambda: False):
            pass


async def test_cancel_interrupts_active_turn(tmp_path):
    interrupted = []
    cancelled = False

    class Turn:
        id = "active"

        async def run(self):
            await asyncio.sleep(10)

        async def interrupt(self):
            interrupted.append(True)

    settings = Settings(_env_file=None, llm_timeout=10)
    client = CodexAgentClient(settings, tmp_path, lambda: cancelled)
    task = asyncio.create_task(client._wait(Turn()))
    await asyncio.sleep(0.01)
    cancelled = True
    with pytest.raises(ReviewCancelled):
        await task
    assert interrupted == [True]
    assert not client._turns


async def test_cancelled_client_never_starts_turn(tmp_path):
    client = CodexAgentClient(Settings(_env_file=None), tmp_path, lambda: True)
    client._entered = True
    with pytest.raises(ReviewCancelled):
        await client.run_agent("test", {}, {})


@pytest.mark.parametrize("raw", ['{"ok":true}', '```json\n{"ok":true}\n```', "{'ok':true}", '{"ok":true,}', '{"ok":true'])
def test_json_syntax_repaired_without_business_field_repair(raw):
    assert parse_agent_output(raw, SCHEMA) == {"ok": True}


@pytest.mark.parametrize("raw", ["", "not json", "{}", "[]", '[{"ok":true}]', '{"unexpected":true}', '{"ok":"yes"}'])
def test_json_empty_nonobject_missing_fields_and_wrong_types_fail(raw):
    with pytest.raises((ValueError, ValidationError)):
        parse_agent_output(raw, SCHEMA)


async def test_isolation_preflight_failure_never_initializes_runtime(tmp_path, monkeypatch):
    from app import codex_client

    async def failed(settings):
        raise codex_client.SandboxUnavailable("隔离预检未通过")

    monkeypatch.setattr(codex_client, "preflight", failed)
    settings = Settings(_env_file=None, llm_base_url="http://localhost:9999/v1", llm_api_key="local", llm_model="local")
    with pytest.raises(AgentError, match="隔离预检"):
        async with CodexAgentClient(settings, tmp_path, lambda: False):
            pytest.fail("must not enter")
    assert list(tmp_path.iterdir()) == []
