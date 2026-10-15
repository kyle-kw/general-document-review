import argparse
import asyncio
from datetime import datetime, timezone
import os
from rq import Queue, SpawnWorker, Worker
from rq.serializers import JSONSerializer
from .config import Settings
from .job_queue import make_connection, worker_name


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--burst", action="store_true")
    parser.add_argument("--health-check", action="store_true")
    parser.add_argument("--preflight", action="store_true", help="Verify real Linux sandbox isolation without starting RQ")
    args = parser.parse_args()
    settings = Settings()
    if not args.health_check:
        from .sandbox import preflight
        asyncio.run(preflight(settings))
        if args.preflight:
            print("Linux sandbox preflight passed")
            return
    with make_connection(settings, blocking=True) as connection:
        name = worker_name(settings)
        if args.health_check:
            heartbeat = connection.hget(f"rq:worker:{name}", "last_heartbeat")
            if not heartbeat:
                raise SystemExit(1)
            last = datetime.fromisoformat(heartbeat.decode().replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - last).total_seconds()
            raise SystemExit(0 if age <= 120 else 1)
        queue = Queue(settings.rq_queue, connection=connection, serializer=JSONSerializer)
        worker_class = SpawnWorker if os.name == "nt" else Worker
        worker = worker_class(
            [queue], connection=connection, name=name, serializer=JSONSerializer,
            worker_ttl=90, job_monitoring_interval=15,
        )
        worker.work(burst=args.burst, logging_level="INFO")


if __name__ == "__main__":
    main()
