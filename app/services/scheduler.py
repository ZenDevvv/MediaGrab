"""Fair-share download scheduling.

Two independent gates:

* a global slot pool (``HOSTED_CONCURRENCY``) so the shared VM never
  over-subscribes its CPU and disk;
* a per-IP cap (``HOSTED_PER_IP_CONCURRENCY``) so one client cannot hold every
  slot while queued jobs from everyone else starve.

The per-IP cap rejects outright rather than queueing, because queueing would let
a single IP accumulate unlimited waiters and monopolize the pool over time.
Requesters past the global cap wait FIFO so the wait order is fair and visible
via ``queue_position``.
"""

import asyncio
from typing import Dict, List, Optional, Tuple

from app import config

__all__ = ["DownloadScheduler", "scheduler", "PER_IP_LIMIT_MESSAGE"]

PER_IP_LIMIT_MESSAGE = (
    "Only 1 concurrent download allowed per IP on hosted server. Please wait for your "
    "previous download to finish, or download the desktop/Android app for unlimited "
    "concurrency."
)


class DownloadScheduler:
    """Global slot pool with FIFO waiting and a per-IP occupancy table."""

    def __init__(self, concurrency: int, per_ip_limit: int):
        # 0 == unlimited on both axes.
        self._concurrency = max(0, concurrency)
        self._per_ip_limit = max(0, per_ip_limit)

        self._active: Dict[str, str] = {}          # job_id -> ip
        self._ip_jobs: Dict[str, List[str]] = {}   # ip -> [job_id, ...]
        self._waiting: List[asyncio.Future] = []
        self._waiter_jobs: Dict[asyncio.Future, str] = {}
        self._lock = asyncio.Lock()

    # -- introspection ----------------------------------------------------
    @property
    def concurrency(self) -> int:
        return self._concurrency

    @property
    def active_slots(self) -> int:
        return len(self._active)

    @property
    def waiting_count(self) -> int:
        return len(self._waiting)

    def queue_position(self, future: asyncio.Future) -> Optional[int]:
        try:
            return self._waiting.index(future) + 1
        except ValueError:
            return None

    def active_for_ip(self, ip: Optional[str]) -> int:
        if not ip:
            return 0
        return len(self._ip_jobs.get(ip, ()))

    def is_limited(self) -> bool:
        return bool(self._concurrency) or bool(self._per_ip_limit)

    # -- gate -------------------------------------------------------------
    async def check_per_ip(self, ip: Optional[str]) -> None:
        """Raise HTTP 429 if this IP already holds its share of active slots."""
        if not self._per_ip_limit or not ip:
            return
        async with self._lock:
            if len(self._ip_jobs.get(ip, ())) >= self._per_ip_limit:
                raise PerIPLimitExceeded(PER_IP_LIMIT_MESSAGE)

    async def acquire(self, job_id: str, ip: Optional[str]) -> Tuple[int, Optional["asyncio.Future"]]:
        """Reserve a slot for ``job_id``.

        Returns ``(queue_position, waiter)``. A slot granted immediately yields
        ``(0, None)``; otherwise the 1-based FIFO position and the future that
        will be resolved when a slot frees up. Callers must follow up with
        :meth:`release`.
        """
        if not self.is_limited():
            return 0, None

        loop = asyncio.get_running_loop()
        async with self._lock:
            if self._per_ip_limit and ip:
                holders = self._ip_jobs.setdefault(ip, [])
                if len(holders) >= self._per_ip_limit:
                    raise PerIPLimitExceeded(PER_IP_LIMIT_MESSAGE)
                holders.append(job_id)

            if not self._concurrency or len(self._active) < self._concurrency:
                self._active[job_id] = ip or ""
                return 0, None

            future: asyncio.Future = loop.create_future()
            self._waiting.append(future)
            self._waiter_jobs[future] = job_id
            return len(self._waiting), future

    async def cancel_waiting(self, job_id: str) -> None:
        """Drop a queued job out of the wait line (e.g. user aborted it)."""
        async with self._lock:
            for future, waiting_job in list(self._waiter_jobs.items()):
                if waiting_job != job_id:
                    continue
                self._discard_waiter(future)
                self._waiter_jobs.pop(future, None)
                if not future.done():
                    future.cancel()

    async def wait_for_slot(self, future: asyncio.Future, job_id: str, ip: Optional[str]) -> None:
        """Await a queued future and claim the slot it was handed."""
        try:
            await future
        except asyncio.CancelledError:
            async with self._lock:
                self._discard_waiter(future)
                self._waiter_jobs.pop(future, None)
            raise
        async with self._lock:
            self._active[job_id] = ip or ""

    async def release(self, job_id: str, ip: Optional[str]) -> None:
        async with self._lock:
            self._active.pop(job_id, None)
            if ip and self._ip_jobs.get(ip):
                holders = self._ip_jobs[ip]
                if job_id in holders:
                    holders.remove(job_id)
                if not holders:
                    del self._ip_jobs[ip]

            # Hand the freed slot to the longest-waiting job, if any.
            while self._waiting:
                nxt = self._waiting.pop(0)
                self._waiter_jobs.pop(nxt, None)
                if not nxt.done():
                    nxt.set_result(None)
                    break
                # Already cancelled -- keep draining.

    def _discard_waiter(self, future: asyncio.Future) -> None:
        try:
            self._waiting.remove(future)
        except ValueError:
            pass

    async def reset(self) -> None:
        async with self._lock:
            for waiter in self._waiting:
                if not waiter.done():
                    waiter.cancel()
            self._waiting.clear()
            self._waiter_jobs.clear()
            self._active.clear()
            self._ip_jobs.clear()


class PerIPLimitExceeded(Exception):
    """Raised when an IP exceeds its concurrent download allowance."""


scheduler = DownloadScheduler(
    concurrency=config.MAX_CONCURRENT_JOBS,
    per_ip_limit=config.PER_IP_CONCURRENCY,
)