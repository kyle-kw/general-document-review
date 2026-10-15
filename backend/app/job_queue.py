from collections.abc import Iterator
import socket
from fastapi import HTTPException, Request
from redis import Redis
from redis.exceptions import RedisError
from rq import Queue
from rq.serializers import JSONSerializer
from .config import Settings


def make_connection(settings: Settings, *, blocking: bool = False) -> Redis:
    url = settings.redis_url.get_secret_value()
    if not url:
        raise ValueError("REDIS_URL must be configured")
    return Redis.from_url(
        url,
        socket_connect_timeout=3,
        socket_timeout=None if blocking else 5,
        health_check_interval=30,
    )


def worker_name(settings: Settings) -> str:
    return f"{settings.rq_queue}-{socket.gethostname()}"


def get_queue(request: Request) -> Iterator[Queue]:
    settings: Settings = request.app.state.settings
    connection = None
    try:
        connection = make_connection(settings)
        yield Queue(
            settings.rq_queue,
            connection=connection,
            serializer=JSONSerializer,
            default_timeout=settings.rq_job_timeout,
        )
    except (RedisError, ValueError) as exc:
        raise HTTPException(503, "Task service unavailable; check Redis configuration") from exc
    finally:
        if connection is not None:
            connection.close()
