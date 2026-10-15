from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app.config import ROOT, Settings
from app.main import create_app


def test_health_and_production_static_routing(tmp_path: Path):
    static = tmp_path / "dist"
    (static / "examples").mkdir(parents=True)
    (static / "index.html").write_text("<html>app</html>", encoding="utf-8")
    (static / "examples" / "test.webp").write_bytes(b"test-image")
    settings = Settings(_env_file=None, app_name="test", data_dir=tmp_path / "data", static_dir=static)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/health").json() == {"status": "ok", "app": "test"}
        assert client.get("/examples/test.webp").content == b"test-image"
        assert client.get("/workspace", headers={"accept": "text/html"}).text == "<html>app</html>"
        assert client.get("/api/missing", headers={"accept": "text/html"}).status_code == 404
        assert client.get("/assets/missing.js", headers={"accept": "text/html"}).status_code == 404
        assert client.get("/examples/missing.webp").status_code == 404


def test_real_frontend_build_is_served(tmp_path: Path):
    static = ROOT / "frontend" / "dist"
    if not (static / "index.html").exists():
        pytest.skip("Run the frontend build to verify real static assets")
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", static_dir=static)
    with TestClient(create_app(settings)) as client:
        assert 'id="root"' in client.get("/").text
        scripts = list((static / "assets").glob("*.js"))
        assert scripts
        assert client.get("/assets/" + scripts[0].name).status_code == 200
