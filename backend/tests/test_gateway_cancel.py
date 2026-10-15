"""Cancellation closes upstream sockets, including stalled header/body reads."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import httpx
import pytest

from app.config import Settings
from app.review_gateway import ReviewGateway


@pytest.mark.parametrize("send_headers", [False, True])
def test_gateway_exit_disconnects_stalled_upstream_promptly(send_headers):
    connected, disconnected = Event(), Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            if send_headers:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'event: response.created\ndata: {"type":"response.created","response":{"output":[]}}\n\n')
                self.wfile.flush()
            self.connection.settimeout(5)
            connected.set()
            try:
                if self.rfile.read(1) == b"":
                    disconnected.set()
            except ConnectionResetError:
                disconnected.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(_env_file=None, llm_base_url=f"http://127.0.0.1:{server.server_port}/v1",
                        llm_api_key="local-test-only", llm_timeout=30)
    gate = ReviewGateway(settings, "requirements")

    def request():
        try:
            with httpx.Client(trust_env=False, timeout=5) as client:
                client.post(gate.base_url + "/responses", headers={"Authorization": "Bearer " + gate.token},
                            json={"tools": [], "input": []})
        except httpx.HTTPError:
            pass  # Interrupted upstream reads intentionally end the stream.

    caller = Thread(target=request, daemon=True)
    try:
        with gate:
            caller.start()
            assert connected.wait(3), "request did not connect"
        assert disconnected.wait(2), "cancel left upstream LLM connection open"
        caller.join(timeout=2)
        assert not caller.is_alive()
        assert gate.client.is_closed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
