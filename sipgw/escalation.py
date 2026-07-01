"""#3 Escalation — alert a human channel when a page cannot be delivered.

Fires on delivery exhaustion ('failed') and staleness ('expired'), posting a
JSON payload to escalation.webhook_url (Teams/Slack/PagerDuty/NOC). Shares the
§2a no-send guarantee: in dry-run the client is built with NoSendGuardTransport,
so the escalation POST cannot reach a real host during testing. Escalation
failures are logged, never raised — they must never disrupt delivery.
"""

import logging
from typing import Dict, Optional

import httpx

from .config import EscalationConfig

logger = logging.getLogger("sipgw.escalation")


class Escalator:
    def __init__(self, config: EscalationConfig, dry_run: bool = False):
        self.config = config
        self._dry_run = dry_run
        self._client: Optional[httpx.AsyncClient] = None
        self._transport = None

    async def initialize(self) -> None:
        from .safety import NoSendGuardTransport
        self._transport = NoSendGuardTransport() if self._dry_run else None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds, connect=5.0),
            transport=self._transport,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def escalate(self, reason: str, row: Dict) -> None:
        """on_escalate(reason, row) callback for the delivery worker.

        Robust by contract: any failure is logged, never raised.
        """
        url = (self.config.webhook_url or "").strip()
        summary = (
            f"sipgw {reason.upper()}: call {row.get('id')} "
            f"{row.get('caller_id')} area={row.get('area_number')} "
            f"room={row.get('room_number')} attempts={row.get('attempts')} "
            f"last_error={row.get('last_error')!r}"
        )

        if not url:
            # No channel configured — still make the failure loud in the log.
            logger.error("ESCALATION (no webhook configured) — %s", summary)
            return

        if self._client is None:
            await self.initialize()

        payload = {
            "reason": reason,
            "call_id": row.get("id"),
            "caller_id": row.get("caller_id"),
            "area_number": row.get("area_number"),
            "room_number": row.get("room_number"),
            "tts": row.get("tts_string"),
            "attempts": row.get("attempts"),
            "fusion_status": row.get("fusion_status"),
            "last_error": row.get("last_error"),
            "text": summary,
        }
        try:
            resp = await self._client.post(url, json=payload)
            logger.error("ESCALATION sent (%s) status=%s — %s",
                         reason, resp.status_code, summary)
        except Exception as e:
            logger.error("ESCALATION POST failed: %s: %s — %s",
                         type(e).__name__, e, summary)
