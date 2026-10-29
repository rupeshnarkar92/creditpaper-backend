"""Redis-only Celery worker liveness; no database heartbeat traffic."""

from __future__ import annotations

import logging
import os
import socket
import threading

from celery.signals import worker_ready, worker_shutdown  # type: ignore[import-untyped]
from redis import Redis

from creditpaper.queueing.config import broker_url

log = logging.getLogger(__name__)

_stop = threading.Event()
_thread: threading.Thread | None = None
_recovery_thread: threading.Thread | None = None


def _client() -> Redis:
    return Redis.from_url(broker_url(), decode_responses=True)


def _key() -> str:
    return f"creditpaper:worker:{socket.gethostname()}:{os.getpid()}"


def _heartbeat() -> None:
    client = _client()
    while not _stop.wait(5):
        try:
            client.set(_key(), "alive", ex=15)
        except Exception as exc:
            log.warning("worker_heartbeat_unavailable error=%s", type(exc).__name__)


def _recovery_loop() -> None:
    """Publish small durable batches continuously without flooding Redis."""
    from creditpaper.queueing.recovery import recover_on_startup

    interval = max(10, int(os.getenv("CELERY_RECOVERY_INTERVAL_SECONDS", "30")))
    client = _client()
    while not _stop.is_set():
        try:
            recover_on_startup(client, _key(), attempts=1)
        except Exception as exc:
            log.warning("worker_recovery_unavailable error=%s", type(exc).__name__)
        if _stop.wait(interval):
            return


@worker_ready.connect  # type: ignore[untyped-decorator]
def start_worker_heartbeat(**_kwargs: object) -> None:
    global _recovery_thread, _thread
    _stop.clear()
    try:
        _client().set(_key(), "alive", ex=15)
    except Exception as exc:
        # Still start the loops: Redis can disappear between broker startup
        # and this signal, and they must recover without a worker restart.
        log.warning("worker_startup_heartbeat_unavailable error=%s", type(exc).__name__)
    _thread = threading.Thread(target=_heartbeat, daemon=True, name="celery-redis-heartbeat")
    _thread.start()
    # Operators can start an isolated worker without replaying the entire
    # durable backlog. This is useful for incident recovery and controlled
    # per-deal validation; PostgreSQL remains the source of truth and the
    # explicit recovery command can publish the backlog later.
    enabled = os.getenv("CELERY_STARTUP_RECOVERY_ENABLED", "true").strip().lower()
    if enabled not in {"0", "false", "no", "off"}:
        _recovery_thread = threading.Thread(
            target=_recovery_loop,
            daemon=True,
            name="celery-bounded-recovery",
        )
        _recovery_thread.start()


@worker_shutdown.connect  # type: ignore[untyped-decorator]
def stop_worker_heartbeat(**_kwargs: object) -> None:
    _stop.set()
    try:
        _client().delete(_key())
    except Exception:
        pass


def workers_alive() -> bool:
    """Return whether at least one local Celery worker heartbeat is fresh."""
    client = _client()
    return any(client.scan_iter(match="creditpaper:worker:*", count=20))
