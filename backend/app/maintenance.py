import asyncio
from datetime import datetime, timezone
import logging

from rq import Queue
from rq.serializers import JSONSerializer

from .config import Settings
from .job_queue import make_connection
from .storage import remove_review
from .store import Store

logger = logging.getLogger(__name__)


def maintain(settings: Settings, store: Store) -> None:
    for identifier in store.expired():
        try:
            store.delete_terminal(identifier, lambda: remove_review(settings.data_dir, identifier), expired_only=True)
        except OSError:
            logger.exception("Cannot clean expired review %s", identifier)
    # Reconcile only when Redis can authoritatively report delivery state.
    try:
        with make_connection(settings) as connection:
            connection.ping()
            queue = Queue(settings.rq_queue, connection=connection, serializer=JSONSerializer)
            for review in store.active():
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(review["updated_at"])).total_seconds()
                if age < 120:
                    continue
                job = queue.fetch_job(review["rq_job_id"])
                status = job.get_status(refresh=True).value if job else None
                if status in {None, "failed", "stopped", "canceled", "finished"}:
                    store.finish_current(review["id"], review["rq_job_id"], settings.retention_days,
                                         error="后台任务已中断，可重试并复用已有检查点")
    except Exception as exc:
        logger.warning("Queue reconciliation unavailable (%s)", type(exc).__name__)


async def maintenance_loop(settings: Settings, store: Store) -> None:
    while True:
        try:
            await asyncio.to_thread(maintain, settings, store)
        except Exception:
            logger.exception("Maintenance failed")
        await asyncio.sleep(settings.maintenance_interval)
