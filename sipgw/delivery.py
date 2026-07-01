"""#2 Durable delivery worker.

Pages are recorded first (state='pending') by the SIP path, then delivered
asynchronously by this worker so a Fusion outage or a crash between record and
send cannot drop a Code Blue. The worker retries with exponential backoff
(honoring Retry-After delta-seconds), escalates on exhaustion, and expires pages
that stay undelivered too long. On startup ``recover()`` returns any
crash-orphaned 'delivering' rows to 'pending' (at-least-once delivery).

Escalation (#3) is injected as ``on_escalate(reason, row)``; when absent the
worker only logs. This module never sends anything itself — it drives
``FusionWebhook``, which carries the §2a no-send guard in dry-run.
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable, Dict, Optional

from .config import DeliveryConfig
from .database import CallDatabase
from .webhook import FusionWebhook

logger = logging.getLogger("sipgw.delivery")

EscalateCb = Callable[[str, Dict], Awaitable[None]]


class DeliveryWorker:
    def __init__(
        self,
        db: CallDatabase,
        webhook: FusionWebhook,
        config: DeliveryConfig,
        on_escalate: Optional[EscalateCb] = None,
        time_func: Callable[[], float] = time.time,
    ):
        self.db = db
        self.webhook = webhook
        self.config = config
        self._on_escalate = on_escalate
        self._time = time_func
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # In-memory per-row cooldown (id -> earliest next-attempt time). Lost on
        # restart, which is fine: recover_inflight re-queues and we retry.
        self._next_before: Dict[int, float] = {}

    def _backoff(self, attempts: int, retry_after: Optional[float]) -> float:
        if retry_after and retry_after > 0:
            return min(retry_after, self.config.max_backoff_seconds)
        delay = self.config.base_backoff_seconds * (2 ** max(0, attempts - 1))
        return min(delay, self.config.max_backoff_seconds)

    async def recover(self) -> int:
        """Return crash-orphaned 'delivering' rows to 'pending'. Startup step."""
        n = await self.db.recover_inflight()
        if n:
            logger.info("recovered %d in-flight row(s) -> pending", n)
        return n

    async def _escalate(self, reason: str, row: Dict) -> None:
        if not self._on_escalate:
            return
        try:
            await self._on_escalate(reason, row)
        except Exception:
            logger.exception("escalation callback failed for call %s", row.get("id"))

    async def process_once(self) -> int:
        """One pass over deliverable rows. Returns the number acted on."""
        batch = await self.db.get_deliverable(limit=self.config.batch_size)
        acted = 0
        for row in batch:
            cid = row["id"]
            now = self._time()

            # Expire pages that have been undelivered too long.
            age = now - row["created_at"]
            if age > self.config.max_age_seconds:
                await self.db.mark_expired(cid)
                self._next_before.pop(cid, None)
                logger.error("call %s EXPIRED after %.0fs undelivered", cid, age)
                await self._escalate("expired", row)
                acted += 1
                continue

            # Respect the in-memory backoff cooldown.
            if now < self._next_before.get(cid, 0.0):
                continue

            attempts = await self.db.mark_attempting(cid)
            status, elapsed = await self.webhook.trigger_scenario(row["tts_string"])
            acted += 1

            if 200 <= status < 300:
                await self.db.mark_delivered(cid, status, elapsed)
                self._next_before.pop(cid, None)
                logger.info("call %s DELIVERED (status %s, attempt %d)",
                            cid, status, attempts)
                continue

            retry_after = getattr(self.webhook, "last_retry_after", None)
            if attempts >= self.config.max_attempts:
                await self.db.mark_failed(
                    cid,
                    last_error=f"exhausted after {attempts} attempts (last status {status})",
                    fusion_status=status,
                )
                self._next_before.pop(cid, None)
                logger.error("call %s FAILED after %d attempts (last status %s)",
                             cid, attempts, status)
                await self._escalate("failed",
                                     {**row, "attempts": attempts, "fusion_status": status})
            else:
                delay = self._backoff(attempts, retry_after)
                self._next_before[cid] = self._time() + delay
                await self.db.reschedule(cid, last_error=f"status {status}",
                                         fusion_status=status)
                logger.warning("call %s retry #%d in %.1fs (status %s%s)",
                               cid, attempts, delay, status,
                               f", Retry-After={retry_after}" if retry_after else "")
        return acted

    async def drain(self, deadline_seconds: float = 10.0) -> None:
        """Best-effort: keep delivering until nothing is pending or the deadline.

        Durability does not depend on this (record-first + recover cover a hard
        stop); it just flushes the queue on a graceful shutdown.
        """
        end = self._time() + deadline_seconds
        while self._time() < end:
            pending = await self.db.get_deliverable(limit=1)
            if not pending:
                return
            await self.process_once()
            await asyncio.sleep(0)

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("delivery loop iteration failed")
            await asyncio.sleep(self.config.poll_interval_seconds)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("delivery worker started (poll=%.1fs, max_attempts=%d)",
                    self.config.poll_interval_seconds, self.config.max_attempts)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("delivery worker stopped")
