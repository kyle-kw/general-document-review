from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import fakeredis
import pytest
from rq import SimpleWorker
from rq.serializers import JSONSerializer
from app.config import Settings
from app.job_queue import worker_name
from app import worker


def test_worker_refuses_queue_before_failed_real_isolation_preflight(monkeypatch):
    from app import sandbox
    async def unavailable(settings):
        raise sandbox.SandboxUnavailable("isolation unavailable")
    monkeypatch.setattr(sandbox, "preflight", unavailable)
    monkeypatch.setattr(worker, "make_connection", lambda *args, **kwargs: pytest.fail("must not connect to Redis"))
    monkeypatch.setattr(sys, "argv", ["worker", "--burst"])
    with pytest.raises(sandbox.SandboxUnavailable, match="isolation unavailable"):
        worker.main()


def test_worker_probe_accepts_real_rq_heartbeat_and_rejects_stale_or_missing(tmp_path: Path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path, rq_queue="probe")
    connection = fakeredis.FakeRedis()
    monkeypatch.setattr(worker, "Settings", lambda: settings)
    monkeypatch.setattr(worker, "make_connection", lambda *args, **kwargs: connection)
    monkeypatch.setattr(sys, "argv", ["worker", "--health-check"])
    active = SimpleWorker(
        ["probe"], connection=connection, name=worker_name(settings),
        serializer=JSONSerializer,
    )
    active.register_birth()
    active.heartbeat()
    with pytest.raises(SystemExit) as healthy:
        worker.main()
    assert healthy.value.code == 0

    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    connection.hset(active.key, "last_heartbeat", stale)
    with pytest.raises(SystemExit) as unhealthy:
        worker.main()
    assert unhealthy.value.code == 1

    connection.delete(active.key)
    with pytest.raises(SystemExit) as missing:
        worker.main()
    assert missing.value.code == 1
