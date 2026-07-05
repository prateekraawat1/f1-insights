"""
cache.py - Redis / FakeRedis cache wrapper for the F1 grid state.

Attempts to connect to a live Redis instance first. If unavailable,
falls back to an in-memory FakeRedis, ensuring the prototype runs
without any external infrastructure dependencies.
"""

import json
import logging
from typing import Any

import redis
import fakeredis

from backend.config import (
    REDIS_HOST,
    REDIS_PORT,
    REDIS_DB,
    REDIS_KEY_PREFIX,
    REDIS_STATE_TTL_S,
    REDIS_MAX_CONNECTIONS,
)

logger = logging.getLogger(__name__)


class CacheClient:
    """
    Thin wrapper around Redis (or FakeRedis) that provides typed
    get/set operations for F1 grid state objects.
    """

    def __init__(self) -> None:
        self._client = self._connect()

    def _connect(self) -> redis.Redis | fakeredis.FakeRedis:
        """
        Try real Redis first; fall back to FakeRedis if unavailable.
        """
        try:
            pool = redis.ConnectionPool(
                host=REDIS_HOST,
                port=REDIS_PORT,
                db=REDIS_DB,
                max_connections=REDIS_MAX_CONNECTIONS,
                socket_connect_timeout=1.0,
                decode_responses=True,
            )
            client = redis.Redis(connection_pool=pool)
            client.ping()
            logger.info("✅ Connected to Redis at %s:%s (pool size %d)", REDIS_HOST, REDIS_PORT, REDIS_MAX_CONNECTIONS)
            return client
        except (redis.ConnectionError, redis.TimeoutError) as exc:
            logger.warning(
                "⚠️  Redis unavailable (%s). Falling back to in-memory FakeRedis.", exc
            )
            return fakeredis.FakeRedis(decode_responses=True)

    # ──────────────────────────── Public API ──────────────────────────────────

    def set_state(self, driver_code: str, state: dict[str, Any]) -> None:
        """Persist a driver's full telemetry state as a JSON hash with TTL."""
        key = f"{REDIS_KEY_PREFIX}{driver_code}"
        self._client.set(key, json.dumps(state), ex=REDIS_STATE_TTL_S)

    def get_state(self, driver_code: str) -> dict[str, Any] | None:
        """Retrieve and deserialise a driver's telemetry state."""
        key = f"{REDIS_KEY_PREFIX}{driver_code}"
        raw = self._client.get(key)
        return json.loads(raw) if raw else None

    def get_all_drivers(self) -> list[str]:
        """Return a list of driver codes currently tracked in the cache."""
        prefix = REDIS_KEY_PREFIX
        keys: list[str] = self._client.keys(f"{prefix}*")
        codes = []
        for k in keys:
            stripped = k.replace(prefix, "")
            # Exclude metadata keys (prefixed with "meta:")
            if not stripped.startswith("meta:"):
                codes.append(stripped)
        return codes

    def set_race_meta(self, key: str, value: Any) -> None:
        """Store a race-level metadata value (e.g. current lap, SC state)."""
        self._client.set(f"{REDIS_KEY_PREFIX}meta:{key}", json.dumps(value))

    def get_race_meta(self, key: str) -> Any | None:
        """Retrieve a race-level metadata value."""
        raw = self._client.get(f"{REDIS_KEY_PREFIX}meta:{key}")
        return json.loads(raw) if raw else None

    def flush(self) -> None:
        """Wipe all F1 state keys (useful between simulation runs)."""
        keys = self._client.keys(f"{REDIS_KEY_PREFIX}*")
        if keys:
            self._client.delete(*keys)
        logger.info("🗑️  Cache flushed.")

    @property
    def backend_type(self) -> str:
        return (
            "Redis" if isinstance(self._client, redis.Redis) else "FakeRedis (in-memory)"
        )


# ─── Module-level singleton ────────────────────────────────────────────────────
cache = CacheClient()
