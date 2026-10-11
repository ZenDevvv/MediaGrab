"""Fair-share download scheduling.

Three independent gates:

* a global slot pool (``HOSTED_CONCURRENCY``) so the shared VM never
  over-subscribes its CPU and disk;
* a **session gate** (1 active download per signed session cookie) -- the limit
  users actually experience, and the one that is fair behind a shared NAT;
* an **IP ceiling** (independent, and never lower than the pool size) so that
  simply dropping the cookie cannot be used to evade the session gate.

Both secondary gates reject outright rather than queueing: queueing would let a
single client accumulate unlimited waiters and monopolize the pool over time.
Requesters past the global cap wait FIFO so the wait order stays fair and
visible via ``queue_position``.
"""

import asyncio
from typing import Dict, List, Optional, Tuple

from app import config

__all__ = [
    "DownloadScheduler",
    "GateExceeded",
    "PER_IP_LIMIT_MESSAGE",
    "PER_SESSION_LIMIT_MESSAGE",
    "scheduler",
]

SESSION_GATE = "session"
IP_GATE = "ip"

PER_SESSION_LIMIT_MESSAGE = (
    "Only 1 concurrent download allowed per session on hosted server. Please wait "
    "for your previous download to finish, or download the desktop/Android app for "
    "unlimited concurrency."
)

PER_IP_LIMIT_MESSAGE = (
    "Too many concurrent downloads from your network. Please wait for an active "
    "download to finish, or download the desktop/Android app for unlimited concurrency."
)


class GateExceeded(Exception):
    """A secondary gate refused the request. Carries which gate tripped."""

    def __init__(self, gate: str, detail: str):
        super().__init__(detail)
        self.gate = gate
        self.detail = detail


class PerSessionLimitExceeded(GateExceeded):
    def __init__(self, detail: str = PER_SESSION_LIMIT_MESSAGE):
        super().__init__(SESSION_GATE, detail)


class PerIPLimitExceeded(GateExceeded):
    def __init__(self, detail: str = PER_IP_LIMIT_MESSAGE):
        super().__init__(IP_GATE, detail)


class DownloadScheduler:
    """Global slot pool with FIFO waiting and dual session/IP occupancy tables."""

    def __init__(
        self,
        concurrency: int,
        per_session_limit: int,
        per_ip_limit: int,
    ):
        # 0 == unlimited on any axis.
        self._concurrency = max(0, concurrency)
        self._per_session_limit = max(0, per_session_limit)
        self._per_ip_limit = max(0, per_ip_limit)

        self._active: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        self._session_jobs: Dict[str, List[str]] = {}  # session_id -> [job_id]
        self._ip_jobs: Dict[str, List[str]] = {}        # ip -> [job_id]
        self._waiting: List[asyncio.Future] = []
        self._waiter_jobs: Dict[asyncio.Future, str] = {}
        self._waiter_identity: Dict[asyncio.Future, Tuple[Optional[str], Optional[str]]] = {}
        self._lock = asyncio.Lock()

    # -- introspection ----------------------------------------------------
    @property
    def concurrency(self) -> int:
        return self._concurrency

    @property
    def per_session_limit(self) -> int:
        return self._per_session_limit

    @property
    def per_ip_limit(self) -> int:
        return self._per_ip_limit

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

    def active_for_session(self, session_id: Optional[str]) -> int:
        if not session_id:
            return 0
        return len(self._session_jobs.get(session_id, ()))

    def is_limited(self) -> bool:
        return bool(self._concurrency or self._per_session_limit or self._per_ip_limit)

    # -- gate -------------------------------------------------------------
    def _check_gates(
        self,
        session_id: Optional[str],
        ip: Optional[str],
    ) -> None:
        """Evaluate both secondary gates. Caller must hold ``self._lock``.

        Order matters: the session gate is reported first because it is the one
        the user can act on, and it is the more specific diagnosis.
        """
        if self._per_session_limit and session_id:
            if len(self._session_jobs.get(session_id, ())) >= self._per_session_limit:
                raise PerSessionLimitExceeded()
        if self._per_ip_limit and ip:
            if len(self._ip_jobs.get(ip, ())) >= self._per_ip_limit:
                raise PerIPLimitExceeded()

    async def check_gates(self, session_id: Optional[str], ip: Optional[str]) -> None:
        """Raise :class:`GateExceeded` if either secondary gate is already full.

        Checked before anything is allocated so a rejected request leaves no job
        record and consumes no slot.
        """
        if not (self._per_session_limit or self._per_ip_limit):
            return
        async with self._lock:
            self._check_gates(session_id, ip)

    async def acquire(
        self,
        job_id: str,
        session_id: Optional[str],
        ip: Optional[str],
    ) -> Tuple[int, Optional["asyncio.Future"]]:
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
            self._check_gates(session_id, ip)

            if self._per_session_limit and session_id:
                self._session_jobs.setdefault(session_id, []).append(job_id)
            if self._per_ip_limit and ip:
                self._ip_jobs.setdefault(ip, []).append(job_id)

            if not self._concurrency or len(self._active) < self._concurrency:
                self._active[job_id] = (session_id, ip)
                return 0, None

            future: asyncio.Future = loop.create_future()
            self._waiting.append(future)
            self._waiter_jobs[future] = job_id
            self._waiter_identity[future] = (session_id, ip)
            return len(self._waiting), future

    async def cancel_waiting(self, job_id: str) -> None:
        """Drop a queued job out of the wait line (e.g. user aborted it)."""
        async with self._lock:
            for future, waiting_job in list(self._waiter_jobs.items()):
                if waiting_job != job_id:
                    continue
                self._discard_waiter(future)
                self._waiter_jobs.pop(future, None)
                session_id, ip = self._waiter_identity.pop(future, (None, None))
                # The gate slots were reserved at acquire() time; release them
                # here or an aborted job would keep occupying its allowance.
                self._release_gate_slots(job_id, session_id, ip)
                if not future.done():
                    future.cancel()

    async def wait_for_slot(
        self,
        future: asyncio.Future,
        job_id: str,
        session_id: Optional[str],
        ip: Optional[str],
    ) -> None:
        """Await a queued future and claim the slot it was handed."""
        try:
            await future
        except asyncio.CancelledError:
            async with self._lock:
                self._discard_waiter(future)
                self._waiter_jobs.pop(future, None)
                queued_session, queued_ip = self._waiter_identity.pop(
                    future, (session_id, ip)
                )
                self._release_gate_slots(job_id, queued_session, queued_ip)
            raise
        async with self._lock:
            self._active[job_id] = (session_id, ip)

    def _release_gate_slots(
        self,
        job_id: str,
        session_id: Optional[str],
        ip: Optional[str],
    ) -> None:
        """Remove a job from both occupancy tables. Caller must hold the lock."""
        if session_id and self._session_jobs.get(session_id):
            holders = self._session_jobs[session_id]
            if job_id in holders:
                holders.remove(job_id)
            if not holders:
                del self._session_jobs[session_id]

        if ip and self._ip_jobs.get(ip):
            holders = self._ip_jobs[ip]
            if job_id in holders:
                holders.remove(job_id)
            if not holders:
                del self._ip_jobs[ip]

    async def release(
        self,
        job_id: str,
        session_id: Optional[str],
        ip: Optional[str],
    ) -> None:
        async with self._lock:
            self._active.pop(job_id, None)
            self._release_gate_slots(job_id, session_id, ip)

            # Hand the freed slot to the longest-waiting job, if any.
            while self._waiting:
                nxt = self._waiting.pop(0)
                self._waiter_jobs.pop(nxt, None)
                self._waiter_identity.pop(nxt, None)
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
            self._waiter_identity.clear()
            self._active.clear()
            self._session_jobs.clear()
            self._ip_jobs.clear()


scheduler = DownloadScheduler(
    concurrency=config.MAX_CONCURRENT_JOBS,
    per_session_limit=config.PER_SESSION_CONCURRENCY,
    per_ip_limit=config.PER_IP_CONCURRENCY,
)