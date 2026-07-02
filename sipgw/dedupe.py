"""#5 Clinical dedupe — SHADOW/DISABLED (ships inert today).

This computes a stable CLINICAL identity for a page — the normalized tuple
(area_number, room_number, bed_number, purpose-derived-from-display_name) — and,
optionally, looks for a recent prior page with the same clinical identity so we
can measure how often true duplicates arrive.

Two hard safety rules:

  1. This is DISTINCT from #15's ``invite_fingerprint`` (sip_message.py). That is
     the SIP TRANSACTION identity (Call-ID/From/CSeq — a retransmit of the SAME
     INVITE). This is the CLINICAL identity (who/where/why). They are two clearly
     named functions and must never be conflated. The version prefix here is
     ``cf-v1:`` (clinical fingerprint) vs ``v1:`` for the transaction one.

  2. It NEVER drops a page today. With the shipped ``DedupeConfig`` defaults
     (``enforce=False``, ``window_seconds=0``) ``evaluate`` does not even query
     the database and always returns a no-suppress decision. A test-only
     ``window_seconds`` > 0 turns on the shadow lookup; when a match is found it
     LOGS 'WOULD suppress ...' but STILL returns no-suppress. Only an
     (out-of-policy, validate_config-forbidden) ``enforce=True`` sets
     ``suppress=True`` — and even then main.py never gates delivery on it.
"""

import time
import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

from .config import DedupeConfig
from .lookups import get_call_purpose

logger = logging.getLogger("sipgw.dedupe")

# Bump when the field set below changes so old log lines stay unambiguous.
# Deliberately different from #15's "v1:" transaction-fingerprint prefix.
_CLINICAL_FP_VERSION = "cf-v1"


def _norm(value: Optional[str]) -> str:
    """Whitespace-normalize a field WITHOUT stripping leading zeros.

    Room/area/bed numbers are leading-zero-significant strings (#? v1.4), e.g.
    room '007' is not room '7'. We only trim surrounding whitespace.
    """
    return (value or "").strip()


def normalize_purpose(display_name: Optional[str]) -> str:
    """Map a SIP display name to its canonical, normalized call purpose.

    Uses the same lookup table as the TTS path so 'Code Blue' vs 'RRT' resolve
    to distinct purposes. Lowercased for a stable, case-insensitive key.
    """
    return get_call_purpose(display_name or "").strip().lower()


def compute_fingerprint(caller) -> str:
    """Stable CLINICAL fingerprint of a page.

    ``caller`` is the ``CallerInfo`` from ``sipgw.parser.parse_caller``. Keyed on
    the normalized tuple (area_number, room_number, bed_number, normalized
    purpose-from-display_name). Leading zeros are preserved (they are clinically
    significant). Same clinical identity -> same fingerprint across calls; a
    different room, bed, or purpose -> a different fingerprint. Never raises.

    This is the CLINICAL identity, intentionally distinct from #15's
    ``invite_fingerprint`` (the SIP transaction identity). Do not unify them.
    """
    try:
        area = _norm(getattr(caller, "area_number", None))
        room = _norm(getattr(caller, "room_number", None))
        bed = _norm(getattr(caller, "bed_number", None))
        purpose = normalize_purpose(getattr(caller, "display_name", "") or "")
    except Exception:
        area = room = bed = purpose = ""

    canonical = "\n".join([
        f"area={area}",
        f"room={room}",
        f"bed={bed}",
        f"purpose={purpose}",
    ])
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{_CLINICAL_FP_VERSION}:{digest}"


@dataclass
class DedupeDecision:
    """Outcome of a dedupe evaluation. ``suppress`` is the ONLY field a caller
    could ever act on, and it is False in every shipped configuration.
    """
    fingerprint: str
    duplicate_of: Optional[int] = None   # prior row id (telemetry only)
    would_suppress: bool = False         # a match was found within the window
    suppress: bool = False               # ALWAYS False unless (forbidden) enforce=True


class Deduper:
    """Evaluates clinical dedupe. Constructed once from ``DedupeConfig``."""

    def __init__(self, config: DedupeConfig):
        self.config = config

    async def evaluate(
        self, db, *, caller, purpose: Optional[str] = None,
        row_id: Optional[int] = None, is_test: int = 0,
        sip_call_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> DedupeDecision:
        """Compute the clinical fingerprint and, if a window is configured,
        look for a recent prior duplicate.

        With the shipped defaults (``window_seconds`` 0) the DB is never touched
        and the decision is fingerprint-only (no suppression). A test-only
        ``window_seconds`` > 0 enables the shadow lookup; a match logs
        'WOULD suppress ...' but the returned decision still does NOT suppress
        unless ``enforce`` is True (which validate_config forbids in prod).

        ``sip_call_id`` (this page's SIP Call-ID, optional) is logged alongside
        the prior page's Call-ID purely to cross-reference the two pages in the
        shadow audit trail; it never affects the decision.

        This method never raises on a lookup failure — it logs and returns a
        no-suppress decision, because dedupe telemetry must never break delivery.
        """
        fp = compute_fingerprint(caller)
        decision = DedupeDecision(fingerprint=fp)

        window = self.config.window_seconds or 0
        if window <= 0:
            # DISABLED (shipped default): never query the DB, never suppress.
            return decision

        if purpose is None:
            purpose = get_call_purpose(getattr(caller, "display_name", "") or "")
        now = time.time() if now is None else now
        since = now - window

        try:
            dup = await db.find_recent_duplicate(
                area_number=getattr(caller, "area_number", None),
                room_number=getattr(caller, "room_number", None),
                bed_number=getattr(caller, "bed_number", None),
                purpose=purpose,
                is_test=is_test,
                since_epoch=since,
                exclude_id=row_id,
                match_bed=self.config.match_bed,
                match_purpose=self.config.match_purpose,
            )
        except Exception:
            logger.exception(
                "dedupe lookup failed (fp=%s, row=%s) — delivering anyway",
                fp, row_id)
            return decision

        if dup is not None:
            # duplicate_of stays the int prior-row id (main.py hands it straight
            # to record_duplicate_of); the richer fields are for the audit line.
            decision.duplicate_of = dup.id
            decision.would_suppress = True
            gap_seconds = now - dup.created_at
            if self.config.enforce:
                # Unreachable in any validated config (validate_config makes
                # enforce=True fatal). Present only so the enforce branch is
                # testable; even so, main.py never gates delivery on it.
                decision.suppress = True
                logger.warning(
                    "SUPPRESS page fp=%s row=%s duplicate_of=%s gap=%.1fs "
                    "bed_match=%s purpose_match=%s this_call_id=%s dup_call_id=%s "
                    "(window=%ss, enforce=ON)",
                    fp, row_id, dup.id, gap_seconds,
                    self.config.match_bed, self.config.match_purpose,
                    sip_call_id, dup.sip_call_id, window)
            else:
                logger.warning(
                    "WOULD suppress page fp=%s row=%s duplicate_of=%s gap=%.1fs "
                    "bed_match=%s purpose_match=%s this_call_id=%s dup_call_id=%s "
                    "(window=%ss) — SHADOW, delivering anyway",
                    fp, row_id, dup.id, gap_seconds,
                    self.config.match_bed, self.config.match_purpose,
                    sip_call_id, dup.sip_call_id, window)

        return decision
