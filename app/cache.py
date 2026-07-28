"""Simple in-memory TTL cache for free-tier rate limits."""
from __future__ import annotations

import time
from threading import Lock
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class TtlCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = Lock()

    def get_or_set(self, key: str, ttl_seconds: float, factory: Callable[[], T]) -> T:
        now = time.time()
        with self._lock:
            hit = self._store.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = factory()
        with self._lock:
            self._store[key] = (now + ttl_seconds, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


cache = TtlCache()
