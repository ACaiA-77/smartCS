"""Application-owned bounded background worker for deferred user-memory candidates.

Chat requests only enqueue durable candidates (``UserMemoryService.process_message``).
This worker owns candidate application: it sweeps claimable owners from durable
storage, claims each owner's candidates through the repository lease
(``claim_token`` + ``claim_until``), applies the memory policy, and releases
failures for a later retry. The database queue — not an in-memory queue or a
same-request task — is the recovery source, so PENDING candidates survive
process crashes and restarts. A lost lease simply expires and another worker
(or the same worker later) retries. No LLM, tool, or business write is ever
replayed by memory failures.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class UserMemoryWorker:
    """Bounded poll/apply loop over the durable user-memory candidate queue.

    Polling uses a short interval while work exists and exponential idle
    backoff (capped) when the queue is empty, so there is never a busy loop.
    ``run_once`` is public so tests and operational scripts can drive exactly
    one bounded cycle deterministically.
    """

    def __init__(
        self,
        service: Any,
        *,
        poll_interval: float = 2.0,
        idle_poll_interval: float = 15.0,
        max_poll_interval: float = 60.0,
        batch_users: int = 20,
        per_user_limit: int = 20,
    ) -> None:
        for name, value in (("poll_interval", poll_interval), ("idle_poll_interval", idle_poll_interval),
                            ("max_poll_interval", max_poll_interval)):
            if type(value) not in (int, float) or value <= 0:
                raise ValueError(f"{name} must be a positive number of seconds")
        if idle_poll_interval > max_poll_interval:
            raise ValueError("idle_poll_interval must not exceed max_poll_interval")
        if type(batch_users) is not int or batch_users <= 0:
            raise ValueError("batch_users must be a positive integer")
        if type(per_user_limit) is not int or per_user_limit <= 0:
            raise ValueError("per_user_limit must be a positive integer")
        self._service = service
        self._poll_interval = float(poll_interval)
        self._idle_poll_interval = float(idle_poll_interval)
        self._max_poll_interval = float(max_poll_interval)
        self._batch_users = batch_users
        self._per_user_limit = per_user_limit
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._wake = asyncio.Event()
        self._stats: dict[str, Any] = {
            "cycles": 0, "users_swept": 0, "claimed_total": 0,
            "accepted_total": 0, "failed_total": 0,
            "last_error_type": None, "started_at": None, "stopped": False,
        }

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def stats(self) -> dict[str, Any]:
        """Numeric-only operational snapshot; never includes memory contents."""
        return dict(self._stats)

    async def start(self) -> None:
        """Idempotently start the background loop on the running event loop."""
        if self.running:
            return
        self._stopping = False
        self._stats.update(started_at=time.time(), stopped=False)
        self._task = asyncio.create_task(self._run(), name="user-memory-worker")

    async def stop(self, timeout: float = 5.0) -> None:
        """Gracefully stop: wake the loop, wait one bounded cycle, then cancel."""
        self._stopping = True
        self._wake.set()
        task, self._task = self._task, None
        if task is None:
            return
        if task.done():
            await task
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            self._stats["stopped"] = True

    async def run_once(self) -> bool:
        """Run one bounded sweep-and-apply cycle; True when any candidate was claimed."""
        sweep = getattr(self._service, "pending_user_ids", None)
        if not callable(sweep):
            return False
        users = await sweep(self._batch_users)
        self._stats["cycles"] += 1
        self._stats["users_swept"] += len(users)
        worked = False
        for user_id in users:
            if self._stopping:
                break
            try:
                result = await self._service.process_pending(user_id, limit=self._per_user_limit)
            except Exception as exc:
                # The durable lease is the retry path; log the type only.
                self._stats["failed_total"] += 1
                self._stats["last_error_type"] = type(exc).__name__
                logger.warning("user-memory apply failed for one owner (%s)", type(exc).__name__)
                continue
            claimed = int(result.get("claimed_count") or 0)
            worked = worked or claimed > 0
            self._stats["claimed_total"] += claimed
            self._stats["accepted_total"] += int(result.get("accepted_count") or 0)
            failed = int(result.get("failed_count") or 0)
            if failed:
                self._stats["failed_total"] += failed
                self._stats["last_error_type"] = "candidate_apply_failed"
                logger.warning("user-memory worker released %d candidate(s) for durable retry", failed)
        return worked

    async def _run(self) -> None:
        idle = self._idle_poll_interval
        while not self._stopping:
            worked = False
            try:
                worked = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._stats["failed_total"] += 1
                self._stats["last_error_type"] = type(exc).__name__
                logger.warning("user-memory worker cycle failed (%s); backing off", type(exc).__name__)
            if self._stopping:
                break
            idle = self._poll_interval if worked else min(idle * 2, self._max_poll_interval)
            delay = self._poll_interval if worked else idle
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            finally:
                self._wake.clear()
        self._stats["stopped"] = True
