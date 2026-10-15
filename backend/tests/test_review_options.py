"""Requirements persistence, compatible API extension, ownership and edit/retry."""
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from app.requirements import ReviewOptions
from app.store import Store
from test_reviews import application, document_bytes


def upload(client, options=None, key="options"):
    return client.post("/api/v1/reviews", files={"main_file": ("主文件.docx", document_bytes())},
                       data={} if options is None else {"review_options": json.dumps(options)},
                       headers={"Idempotency-Key": key})


def test_options_are_durable_and_changed_only_with_terminal_retry(application):
    options = {"custom_points": ["合同应包含付款条件"], "review_notes": "金额以万元为单位"}
    with TestClient(application) as client:
        client.post("/api/v1/session")
        created = upload(client, options).json()
        identifier = created["id"]
        assert created["review_options"] == options
        assert created["review_requirements"]["status"] == "pending"
        assert upload(client, {"custom_points": ["different"]}).json()["id"] == identifier
        changed = {"custom_points": ["合同应包含违约条款"], "review_notes": ""}
        endpoint = f"/api/v1/reviews/{identifier}/retry"
        assert client.post(endpoint, json={"review_options": changed}).status_code == 409
        application.state.store.finish(identifier, "failed", 30, error="要求未通过")
        response = client.post(endpoint, json={"review_options": changed})
        assert response.status_code == 202, response.text
        assert response.json()["attempt"] == 2
        assert response.json()["review_options"] == changed
        assert response.json()["review_requirements"]["normalized"] is None
        assert response.json()["files"] == created["files"]
        assert client.post(endpoint, json={"review_options": options}).status_code == 409
        assert application.state.store.get(identifier)["review_options"] == changed
        application.state.store.finish(identifier, "cancelled", 30)
        assert client.post(endpoint).json()["review_options"] == changed


@pytest.mark.parametrize("options", [{"custom_points": ["x"] * 11}, {"custom_points": ["x" * 501]},
                                     {"custom_points": [" "]}, {"review_notes": "x" * 2001},
                                     {"system_prompt": "override"}, []])
def test_options_validation_rejects_without_creating_task(application, options):
    with TestClient(application) as client:
        client.post("/api/v1/session")
        assert upload(client, options).status_code == 422
        assert client.get("/api/v1/reviews").json()["total"] == 0


def test_legacy_submit_and_cross_browser_edit_isolation(application):
    with TestClient(application) as first, TestClient(application) as second:
        first.post("/api/v1/session")
        second.post("/api/v1/session")
        item = upload(first).json()
        assert item["review_options"] == ReviewOptions().model_dump()
        assert item["review_requirements"]["status"] == "not_required"
        application.state.store.finish(item["id"], "failed", 30)
        response = second.post(f"/api/v1/reviews/{item['id']}/retry",
                               json={"review_options": {"custom_points": ["其他浏览器尝试更改"]}})
        assert response.status_code == 404
        assert application.state.store.get(item["id"])["review_options"] == ReviewOptions().model_dump()


@pytest.mark.parametrize("status", ["completed", "partial", "failed", "cancelled"])
def test_every_terminal_state_can_edit_requirements_and_retry(application, status):
    with TestClient(application) as client:
        client.post("/api/v1/session")
        item = upload(client).json()
        application.state.store.finish(item["id"], status, 30)
        options = {"custom_points": ["应列明付款条件"], "review_notes": "区分含税金额"}
        response = client.post(f"/api/v1/reviews/{item['id']}/retry", json={"review_options": options})
        assert response.status_code == 202, response.text
        assert response.json()["review_options"] == options
        assert response.json()["review_requirements"]["status"] == "pending"
        assert response.json()["attempt"] == 2


def test_v1_database_migration_preserves_tasks_issues_and_is_idempotent(tmp_path):
    store = Store(tmp_path / "app.sqlite3")
    store.initialize()
    owner = store.add_owner("test-token")
    assert store.create("legacy", owner, "key", [{"filename": "原文.docx", "id": "main", "path": "originals/main.docx"}])
    store.save_issues("legacy", [{"id": "existing-issue", "category": "typo"}])
    # Reconstruct the previous schema, which predates both JSON columns.
    with sqlite3.connect(store.path) as db:
        db.execute("ALTER TABLE reviews DROP COLUMN review_options")
        db.execute("ALTER TABLE reviews DROP COLUMN review_requirements")
        db.execute("PRAGMA user_version=1")
    store.initialize()
    store.initialize()
    item = store.get("legacy", owner)
    assert item["title"] == "原文.docx" and item["issue_count"] == 1
    assert item["review_options"] == ReviewOptions().model_dump()
    assert item["review_requirements"]["status"] == "not_required"
    assert store.issues("legacy")["items"][0]["id"] == "existing-issue"
    assert store.owner("test-token") == owner
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        db.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="newer"):
        store.initialize()
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99
