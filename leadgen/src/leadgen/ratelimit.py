"""Rate limiting.

Two limiters, both required:

* A **global** bucket keeps total outbound throughput under control so the
  pipeline doesn't saturate its own network or trip a provider's account-wide
  quota.
* A **per-host** bucket keeps us from hammering one small business's shared
  hosting. A local plumber's site may be on a $5/month VPS; 20 concurrent
  requests is a denial of service, not a crawl.

Both are token buckets rather than fixed windows, because bursts at a window
boundary are exactly the pattern that gets a crawler blocked.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict


class TokenBucket:
    """Async token bucket. ``rate`` tokens per second, capped at ``capacity``."""

    __slots__ = ("_lock", "_tokens", "_updated", "capacity", "rate")

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(rate, 1.0)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self.rate
            # Sleep outside the lock so other callers can top up their own
            # accounting instead of serializing behind this one.
            await asyncio.sleep(wait)


class HostRateLimiter:
    """One bucket per hostname, created on demand.

    Buckets are never evicted during a run. At a few thousand hosts that is a
    few hundred kilobytes — cheaper than the bookkeeping to expire them, and
    the process is short-lived.
    """

    def __init__(self, rps: float, global_rps: float) -> None:
        self._rps = rps
        self._hosts: dict[str, TokenBucket] = {}
        self._global = TokenBucket(global_rps, capacity=global_rps * 2)
        self._lock = asyncio.Lock()

    async def acquire(self, host: str) -> None:
        await self._global.acquire()
        async with self._lock:
            bucket = self._hosts.get(host)
            if bucket is None:
                # Capacity 1 = no burst allowance per host. Deliberate: the
                # point is to never send two requests back-to-back to a small
                # site, and a burst allowance would permit exactly that.
                bucket = TokenBucket(self._rps, capacity=1.0)
                self._hosts[host] = bucket
        await bucket.acquire()

    @property
    def tracked_hosts(self) -> int:
        return len(self._hosts)


class ConcurrencyLimiter:
    """Semaphore wrapper that also reports live utilization for logging."""

    def __init__(self, limit: int) -> None:
        self._sem = asyncio.Semaphore(limit)
        self.limit = limit
        self.in_flight = 0
        self.peak = 0

    async def __aenter__(self) -> ConcurrencyLimiter:
        await self._sem.acquire()
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.in_flight -= 1
        self._sem.release()


class QuotaCounter:
    """Counts billable API calls so a run can stop before a bill surprises you."""

    def __init__(self, limits: dict[str, int] | None = None) -> None:
        self.counts: dict[str, int] = defaultdict(int)
        self.limits = limits or {}

    def increment(self, key: str, n: int = 1) -> None:
        self.counts[key] += n

    def exceeded(self, key: str) -> bool:
        limit = self.limits.get(key)
        return limit is not None and self.counts[key] >= limit

    def remaining(self, key: str) -> int | None:
        limit = self.limits.get(key)
        return None if limit is None else max(0, limit - self.counts[key])

    def as_dict(self) -> dict[str, int]:
        return dict(self.counts)
