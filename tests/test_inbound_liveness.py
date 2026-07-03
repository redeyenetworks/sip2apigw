"""inbound-liveness tests: Rauland-reachability monitor.

Covers the sibling-of-#7 INBOUND-direction feature:
  * SIP receive-path stamp is additive and allowlist-scoped (only Rauland-network
    datagrams reset the clock; response codes are byte-for-byte unchanged);
  * DB persistence round-trips, UPSERTs in place, and tolerates a missing table;
  * /health surfaces last_inbound_sip_age_s as INFORMATIONAL only — an ANCIENT
    inbound never flips the status code (still heartbeat-driven);
  * the writer flush loop persists the in-memory epoch and NEVER clobbers the
    persisted value across a restart (writes nothing when never-seen-since-boot);
  * optional silence escalation fires once per episode, resets on fresh inbound,
    defaults OFF, and in dry-run reaches no real host (no-send guard);
  * validate_config warns on a too-low escalation threshold.
"""

import asyncio
import logging
import time

import httpx
import pytest
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from sipgw.config import (
    AppConfig, DashboardConfig, EscalationConfig, HealthConfig, validate_config,
)
from sipgw.database import CallDatabase
from sipgw.dashboard import create_dashboard
from sipgw.escalation import Escalator
from sipgw.main import SIPGateway
from sipgw.sip_message import parse_sip_message
from sipgw.sip_server import SIPServer


# ----------------------------------------------------------------- SIP helpers
def _options(from_user="a730r201") -> bytes:
    msg = (
        f"OPTIONS sip:gw@127.0.0.1:5060 SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP 127.0.0.1:5061;branch=z9hG4bK-opt\r\n"
        f'From: "Code Blue" <sip:{from_user}@127.0.0.1>;tag=t1\r\n'
        f"To: <sip:gw@127.0.0.1:5060>\r\n"
        f"Call-ID: opt-1@h\r\n"
        f"CSeq: 1 OPTIONS\r\n\r\n"
    )
    return msg.encode()


def _sip_200_response() -> bytes:
    # A SIP *response* (not a request) — returns before the allowlist stamp.
    msg = (
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/UDP 127.0.0.1:5061;branch=z9hG4bK-r\r\n"
        "From: <sip:gw@127.0.0.1>;tag=a\r\n"
        "To: <sip:x@127.0.0.1>;tag=b\r\n"
        "Call-ID: r-1@h\r\nCSeq: 1 BYE\r\nContent-Length: 0\r\n\r\n"
    )
    return msg.encode()


class _FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append(data)


def _server(allowed=("127.0.0.0/8",)):
    config = AppConfig()
    config.sip.allowed_networks = list(allowed)
    return SIPServer(config=config, on_call=AsyncMock())


# ---------------------------------------------------------------- SIP stamping
class TestSipStamp:
    @pytest.mark.asyncio
    async def test_allowed_request_stamps(self):
        srv = _server(allowed=("127.0.0.0/8",))
        assert srv.last_inbound_at is None
        t0 = time.time()
        tr = _FakeTransport()
        await srv.handle_message(_options(), ("127.0.0.1", 5060), tr, "udp")
        # Stamp set, and the OPTIONS response is unchanged (200).
        assert srv.last_inbound_at is not None and srv.last_inbound_at >= t0
        assert parse_sip_message(tr.sent[0]).status_code == 200

    @pytest.mark.asyncio
    async def test_disallowed_request_does_not_stamp(self):
        # Only 172.16/12 allowed -> a 127.0.0.1 datagram is rejected (403) and
        # MUST NOT stamp, so scanner noise cannot mask a real Rauland outage.
        srv = _server(allowed=("172.16.0.0/12",))
        tr = _FakeTransport()
        await srv.handle_message(_options(), ("127.0.0.1", 5060), tr, "udp")
        assert srv.last_inbound_at is None                      # never stamped
        assert parse_sip_message(tr.sent[0]).status_code == 403  # unchanged

    @pytest.mark.asyncio
    async def test_response_datagram_does_not_stamp(self):
        # A SIP response (not a request) returns before the allowlist -> no stamp.
        srv = _server(allowed=("127.0.0.0/8",))
        tr = _FakeTransport()
        await srv.handle_message(_sip_200_response(), ("127.0.0.1", 5060), tr, "udp")
        assert srv.last_inbound_at is None
        assert tr.sent == []                                    # responses ignored

    @pytest.mark.asyncio
    async def test_stamp_advances_on_each_allowed_datagram(self):
        srv = _server()
        tr = _FakeTransport()
        await srv.handle_message(_options(), ("127.0.0.1", 5060), tr, "udp")
        first = srv.last_inbound_at
        await asyncio.sleep(0.01)
        await srv.handle_message(_options(), ("127.0.0.1", 5060), tr, "udp")
        assert srv.last_inbound_at >= first


# ------------------------------------------------------------------- DB layer
class TestInboundSeenDB:
    @pytest.mark.asyncio
    async def test_round_trip_and_upsert(self, tmp_path):
        db = CallDatabase(str(tmp_path / "i.db"))
        await db.initialize()
        assert await db.read_inbound_seen() is None            # never stamped
        epoch = 1700000000.5
        back = await db.write_inbound_seen(epoch)
        assert back == epoch
        assert await db.read_inbound_seen() == epoch
        # UPSERT in place: a later epoch replaces the single row.
        newer = epoch + 60
        await db.write_inbound_seen(newer)
        assert await db.read_inbound_seen() == newer
        await db.close()

    @pytest.mark.asyncio
    async def test_does_not_collide_with_writer_heartbeat(self, tmp_path):
        # inbound_sip and the writer heartbeat share the table but NOT the row:
        # writing one must not disturb the other (the /health status reader).
        db = CallDatabase(str(tmp_path / "i.db"))
        await db.initialize()
        await db.write_heartbeat("writer")
        await db.write_inbound_seen(1700000000.0)
        beat = await db.read_heartbeat("writer")
        assert beat is not None and abs(beat - 1700000000.0) > 1  # untouched
        assert await db.read_inbound_seen() == 1700000000.0
        await db.close()

    @pytest.mark.asyncio
    async def test_reader_query_only_can_read(self, tmp_path):
        p = str(tmp_path / "c.db")
        w = CallDatabase(p)
        await w.initialize()
        await w.write_inbound_seen(1700000123.0)
        await w.close()
        r = CallDatabase(p, read_only=True)
        await r.initialize()
        assert await r.read_inbound_seen() == 1700000123.0
        await r.close()

    @pytest.mark.asyncio
    async def test_read_tolerates_missing_table(self, tmp_path):
        db = CallDatabase(str(tmp_path / "i.db"))
        await db.initialize()
        await db._db.execute("DROP TABLE heartbeat")
        await db._db.commit()
        assert await db.read_inbound_seen() is None            # degrades, no raise
        await db.close()


# --------------------------------------------------------- /health informational
def _health_client(beat, inbound=None, stale_after=30.0):
    db = AsyncMock(spec=CallDatabase)
    db.read_heartbeat = AsyncMock(return_value=beat)
    db.read_fusion_check = AsyncMock(return_value=None)
    db.read_inbound_seen = AsyncMock(return_value=inbound)
    db.delivery_health_snapshot = AsyncMock(return_value={
        "backlog": 0, "last_delivered_at": None,
        "last_failed_at": None, "last_error": None})
    app = create_dashboard(db, DashboardConfig(),
                           health_config=HealthConfig(stale_after_seconds=stale_after))
    return TestClient(app)


class TestHealthInformational:
    def test_ancient_inbound_still_200_and_surfaced(self):
        # 10-day-old inbound + FRESH heartbeat -> still 200, age surfaced.
        ancient = time.time() - 10 * 86400
        r = _health_client(time.time(), inbound=ancient).get("/health")
        j = r.json()
        assert r.status_code == 200 and j["status"] == "ok"
        assert j["last_inbound_sip_age_s"] >= 10 * 86400 - 5
        assert j["last_inbound_sip_at"] == ancient

    def test_inbound_age_never_flips_status_when_heartbeat_stale(self):
        # Stale heartbeat -> 503 regardless of a FRESH inbound stamp.
        r = _health_client(time.time() - 999, inbound=time.time()).get("/health")
        assert r.status_code == 503 and r.json()["status"] == "stale"

    def test_never_seen_inbound_reports_none(self):
        r = _health_client(time.time(), inbound=None).get("/health")
        j = r.json()
        assert r.status_code == 200
        assert j["last_inbound_sip_at"] is None
        assert "last_inbound_sip_age_s" not in j


# --------------------------------------------------------------- flush loop
def _dry_gateway(tmp_path):
    config = AppConfig()
    config.fusion.dry_run = True
    config.database.path = str(tmp_path / "gw.db")
    config.health.inbound_flush_interval_seconds = 0.01
    return SIPGateway(config)


async def _run_flush_once(gw):
    task = asyncio.create_task(gw._inbound_flush_loop())
    await asyncio.sleep(0.05)          # let the first iteration run
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestFlushLoop:
    @pytest.mark.asyncio
    async def test_flush_persists_in_memory_epoch(self, tmp_path):
        gw = _dry_gateway(tmp_path)
        await gw.db.initialize()
        try:
            gw.sip_server._last_inbound_at = 1700000999.0
            await _run_flush_once(gw)
            assert await gw.db.read_inbound_seen() == 1700000999.0
        finally:
            await gw.db.close()

    @pytest.mark.asyncio
    async def test_restart_never_clobbers_persisted_value(self, tmp_path):
        # A persisted real value survives a writer restart: with no datagram since
        # boot (in-memory None) the loop must write NOTHING.
        gw = _dry_gateway(tmp_path)
        await gw.db.initialize()
        try:
            await gw.db.write_inbound_seen(1699999000.0)   # pre-restart real value
            assert gw.sip_server.last_inbound_at is None    # fresh boot, no datagram
            await _run_flush_once(gw)
            assert await gw.db.read_inbound_seen() == 1699999000.0  # untouched
        finally:
            await gw.db.close()


# --------------------------------------------------------------- escalation
class TestSilenceEscalation:
    @pytest.mark.asyncio
    async def test_fires_once_per_episode_and_resets(self, tmp_path):
        gw = _dry_gateway(tmp_path)
        gw.escalator = AsyncMock(spec=Escalator)
        ancient = time.time() - 10 * 86400
        # First check on this episode -> escalate once.
        await gw._maybe_escalate_inbound_silence(ancient, escalate_after=432000.0)
        assert gw.escalator.escalate.await_count == 1
        # Same reference epoch -> NOT re-fired (de-duped).
        await gw._maybe_escalate_inbound_silence(ancient, escalate_after=432000.0)
        assert gw.escalator.escalate.await_count == 1
        # A fresher (but still ancient) datagram = new episode -> escalate again.
        newer_but_old = time.time() - 6 * 86400
        await gw._maybe_escalate_inbound_silence(newer_but_old, escalate_after=432000.0)
        assert gw.escalator.escalate.await_count == 2
        reason = gw.escalator.escalate.await_args.args[0]
        assert reason == "inbound-silence"

    @pytest.mark.asyncio
    async def test_fresh_inbound_does_not_escalate(self, tmp_path):
        gw = _dry_gateway(tmp_path)
        gw.escalator = AsyncMock(spec=Escalator)
        await gw._maybe_escalate_inbound_silence(time.time(), escalate_after=432000.0)
        gw.escalator.escalate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_default_off_never_escalates(self, tmp_path):
        # inbound_escalate_after_seconds defaults 0 -> the loop never even checks.
        gw = _dry_gateway(tmp_path)
        assert gw.config.health.inbound_escalate_after_seconds == 0.0
        gw.escalator = AsyncMock(spec=Escalator)
        await gw.db.initialize()
        try:
            gw.sip_server._last_inbound_at = time.time() - 30 * 86400  # very stale
            await _run_flush_once(gw)
            gw.escalator.escalate.assert_not_awaited()
        finally:
            await gw.db.close()

    @pytest.mark.asyncio
    async def test_restart_fallback_reads_persisted_epoch(self, tmp_path):
        # After a restart in-memory is None; the escalation reference falls back to
        # the persisted epoch so a silence that began pre-restart still alerts.
        gw = _dry_gateway(tmp_path)
        gw.escalator = AsyncMock(spec=Escalator)
        await gw.db.initialize()
        try:
            await gw.db.write_inbound_seen(time.time() - 10 * 86400)
            await gw._maybe_escalate_inbound_silence(None, escalate_after=432000.0)
            assert gw.escalator.escalate.await_count == 1
        finally:
            await gw.db.close()

    @pytest.mark.asyncio
    async def test_dry_run_reaches_no_real_host_and_never_raises(self, tmp_path):
        # The escalation inherits the #3 no-send guard: in dry-run the POST is
        # refused (recorded in blocked), nothing forwarded, and it never raises.
        gw = _dry_gateway(tmp_path)
        gw.escalator = Escalator(
            EscalationConfig(webhook_url="https://hooks.example.com/escalation"),
            dry_run=True)
        await gw.escalator.initialize()
        try:
            ancient = time.time() - 10 * 86400
            await gw._maybe_escalate_inbound_silence(ancient, escalate_after=432000.0)
            assert gw.escalator._transport is not None
            assert gw.escalator._transport.forwarded == []
            assert any("escalation" in httpx.URL(u).path
                       for _m, u in gw.escalator._transport.blocked)
        finally:
            await gw.escalator.close()


# --------------------------------------------------------------- config guard
class TestConfigGuard:
    def _prod(self):
        c = AppConfig()
        from sipgw.config import FusionConfig
        c.fusion = FusionConfig(
            base_url="https://api.icmobile.singlewire.com/api",
            token_url="https://api.icmobile.singlewire.com/api/token",
            audience="prov", scenario_id="scen", scenario_field_id="field",
            client_id="cid", client_secret="secret")
        c.escalation.webhook_url = "https://hooks.example.com/escalation"
        return c

    def test_default_off_is_inert(self):
        c = self._prod()
        assert all("inbound_escalate" not in w for w in validate_config(c, dry_run=False))

    def test_too_low_threshold_warns(self):
        c = self._prod()
        c.health.inbound_escalate_after_seconds = 3600.0  # 1h, below the ~5d floor
        warns = validate_config(c, dry_run=False)
        assert any("inbound_escalate_after_seconds" in w for w in warns)

    def test_generous_threshold_passes_clean(self):
        c = self._prod()
        c.health.inbound_escalate_after_seconds = 604800.0  # 7 days
        assert all("inbound_escalate" not in w for w in validate_config(c, dry_run=False))
