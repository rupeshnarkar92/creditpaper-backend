"""Redis-backed expiring concurrency slots.

The sorted-set lease is intentionally advisory. PostgreSQL fencing and
idempotent writes remain authoritative; this layer only prevents one tenant or
the local LLM from consuming the whole worker fleet.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from redis import Redis

from creditpaper.queueing.config import broker_url

log = logging.getLogger(__name__)

_RENEW = """
local expiry = redis.call('ZSCORE', KEYS[1], ARGV[3])
if not expiry or tonumber(expiry) <= tonumber(ARGV[1]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return 1
"""

_ACQUIRE = """
local now = tonumber(ARGV[1])
local expires = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZSCORE', KEYS[1], ARGV[4]) then
  redis.call('ZADD', KEYS[1], expires, ARGV[4])
  redis.call('EXPIRE', KEYS[1], math.ceil((expires-now)/1000)+5)
  return 1
end
if redis.call('ZCARD', KEYS[1]) >= limit then return 0 end
redis.call('ZADD', KEYS[1], expires, ARGV[4])
redis.call('EXPIRE', KEYS[1], math.ceil((expires-now)/1000)+5)
return 1
"""


@dataclass
class SlotUnavailable(RuntimeError):
    scope: str


class RedisLeaseLimiter:
    def __init__(self, client: Redis, *, namespace: str = "creditpaper:slots") -> None:
        self.client = client
        self.namespace = namespace

    def _key(self, scope: str) -> str:
        return f"{self.namespace}:{scope}"

    def acquire(self, scope: str, token: str, limit: int, lease_s: int) -> bool:
        now = int(time.time() * 1000)
        return bool(
            self.client.eval(
                _ACQUIRE,
                1,
                self._key(scope),
                now,
                now + lease_s * 1000,
                limit,
                token,
            )
        )

    def renew(self, scope: str, token: str, lease_s: int) -> bool:
        now = int(time.time() * 1000)
        return bool(
            self.client.eval(
                _RENEW,
                1,
                self._key(scope),
                now,
                now + lease_s * 1000,
                token,
                lease_s + 5,
            )
        )

    def release(self, scope: str, token: str) -> None:
        self.client.zrem(self._key(scope), token)

    @contextmanager
    def slot(self, scope: str, token: str, limit: int, lease_s: int) -> Iterator[None]:
        if not self.acquire(scope, token, limit, lease_s):
            raise SlotUnavailable(scope)
        stop = threading.Event()

        def keep_alive() -> None:
            while not stop.wait(max(1, lease_s // 3)):
                try:
                    if not self.renew(scope, token, lease_s):
                        log.error("redis_slot_ownership_lost scope=%s", scope)
                        return
                except Exception as exc:
                    log.warning("redis_slot_renew_failed scope=%s error=%s", scope, type(exc).__name__)

        thread = threading.Thread(target=keep_alive, daemon=True, name="redis-slot-renewal")
        thread.start()
        try:
            yield
        finally:
            stop.set()
            self.release(scope, token)


def limiter() -> RedisLeaseLimiter:
    from creditpaper.queueing.config import broker_url

    return RedisLeaseLimiter(Redis.from_url(broker_url(), decode_responses=True))


def tenant_limit() -> int:
    return max(2, int(os.getenv("CELERY_TENANT_CONCURRENCY", "4")))


def tenant_execution_limit(tenant_id: str, kind: str) -> int:
    """Return the aggregate ceiling shared by every lane for one tenant.

    Lane reservation belongs in ``tenant_queue_limit``. Supplying a smaller
    limit while acquiring this shared Redis set accidentally lowered the total
    ceiling whenever a parse task arrived, allowing only two parsers beside
    intake. The independent parse lane cap prevents parsers consuming the
    fourth slot; the shared set must consistently use the same ceiling.
    """
    del tenant_id, kind
    return tenant_limit()


def tenant_queue_limit(kind: str | None = None) -> int:
    """Per-lane cap nested inside the aggregate per-tenant cap.

    Defaults to one execution in each lane so native parsing cannot consume a
    tenant's entire aggregate budget. Operators can raise individual lanes
    after measuring database and CPU headroom, for example
    ``CELERY_TENANT_PARSE_CONCURRENCY=2``.
    """
    lane = ""
    if kind:
        from creditpaper.queueing.config import queue_for_kind

        lane = queue_for_kind(kind).rsplit(".", 1)[-1].replace("-", "_").upper()
    configured = os.getenv(f"CELERY_TENANT_{lane}_CONCURRENCY") if lane else None
    if configured:
        return max(1, int(configured))
    generic = os.getenv("CELERY_TENANT_QUEUE_CONCURRENCY")
    if generic:
        return max(1, int(generic))
    # Keep one aggregate tenant slot unavailable to parse. Intake has its own
    # lane cap of one and can therefore always use that remaining shared slot.
    return max(1, tenant_limit() - 1) if lane == "PARSE" else 1


def llm_limit() -> int:
    return max(1, int(os.getenv("CELERY_LLM_CONCURRENCY", os.getenv("OLLAMA_MAX_CONCURRENCY", "1"))))


def lease_seconds() -> int:
    return max(30, int(os.getenv("CELERY_TENANT_LEASE_S", "120")))
