"""Celery application for local-first document processing."""

from __future__ import annotations

from celery import Celery  # type: ignore[import-untyped]
from kombu import Exchange, Queue  # type: ignore[import-untyped]

from creditpaper.queueing import health as _health  # noqa: F401 - registers Celery signals
from creditpaper.queueing.config import (
    FACTS_QUEUE,
    GENERATION_QUEUE,
    INTAKE_QUEUE,
    OCR_QUEUE,
    PARSE_QUEUE,
    broker_url,
    result_backend,
)


def _queue(name: str) -> Queue:
    return Queue(name, Exchange(name, type="direct"), routing_key=name)


app = Celery("creditpaper", broker=broker_url(), backend=result_backend())
app.conf.update(
    task_queues=tuple(
        _queue(name) for name in (INTAKE_QUEUE, PARSE_QUEUE, OCR_QUEUE, FACTS_QUEUE, GENERATION_QUEUE)
    ),
    task_default_queue=FACTS_QUEUE,
    task_default_exchange=FACTS_QUEUE,
    task_default_routing_key=FACTS_QUEUE,
    task_serializer="json",
    result_serializer="json",
    accept_content=("json",),
    task_acks_late=True,
    task_acks_on_failure_or_timeout=False,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 7200},
    result_expires=3600,
    timezone="UTC",
    enable_utc=True,
    imports=("creditpaper.queueing.tasks",),
)
