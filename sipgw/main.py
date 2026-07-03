"""Main entry point for sipgw — SIP-to-Webhook Gateway (writer process).

Wires together the SIP server + delivery worker + heartbeat + watchdog. As of
#14 (two-service split) the dashboard runs in its OWN process
(``python -m sipgw.dashboard_app``); this process no longer serves HTTP. It
KEEPS writing the heartbeat row, which the decoupled dashboard reads for /health.
"""

import asyncio
import logging
import signal
import sys
import os

from .config import load_config, AppConfig
from .lookups import load_lookups
from .logging_config import setup_logging
from .database import CallDatabase
from .webhook import FusionWebhook
from .parser import parse_caller
from .tts_builder import build_tts, assemble_tts
from .sip_server import SIPServer
from .delivery import DeliveryWorker
from .escalation import Escalator
from .dedupe import Deduper
from .watchdog import notify_ready, notify_stopping, WatchdogPinger
from .safety import effective_dry_run

logger = logging.getLogger("sipgw.main")


class SIPGateway:
    """Top-level application that coordinates all sipgw components."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._dry_run = effective_dry_run(config.fusion.dry_run)
        # dry_run feeds the prod-DB barrier, which runs on every DB open.
        self.db = CallDatabase(config.database.path, timezone=config.logging.timezone,
                               dry_run=self._dry_run)
        self.webhook = FusionWebhook(config.fusion)
        # #3 escalation, shares the no-send guard in dry-run.
        self.escalator = Escalator(config.escalation, dry_run=self._dry_run)
        # #2 durable delivery escalates via the Escalator on failed/expired.
        self.worker = DeliveryWorker(self.db, self.webhook, config.delivery,
                                     on_escalate=self.escalator.escalate)
        # #5 clinical dedupe — SHADOW/DISABLED. Constructed once; used AFTER the
        # record-first insert as pure telemetry. It never gates delivery.
        self.deduper = Deduper(config.dedupe)
        self.sip_server = SIPServer(config=config, on_call=self.on_call)
        # #14: the dashboard now runs as a separate process (sipgw.dashboard_app);
        # this writer process no longer serves HTTP.
        self._hb_task = None           # #7 heartbeat writer
        self._keepalive_task = None    # #7 Fusion reachability keepalive
        self.watchdog = WatchdogPinger()   # #8 systemd watchdog (inert w/o systemd)
        self._shutdown_event = asyncio.Event()

    async def _heartbeat_loop(self):
        interval = self.config.health.heartbeat_interval_seconds
        while True:
            try:
                await self.db.write_heartbeat("writer")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("heartbeat write failed")
            await asyncio.sleep(interval)

    async def _keepalive_loop(self):
        """#7 Periodically probe Fusion reachability (READ-ONLY) and stamp the
        result to the DB for the /health INFORMATIONAL fields.

        Modeled on _heartbeat_loop: a bounded check_reachable() GET every
        keepalive_interval_seconds. It NEVER sends a page and NEVER gates
        /health. All exceptions are guarded so the probe can never crash the
        writer or block the page path. In dry-run the shared no-send guard means
        the probe reaches no real host.
        """
        interval = getattr(self.config.health, "keepalive_interval_seconds", 300.0)
        while True:
            try:
                ok, detail = await self.webhook.check_reachable()
                await self.db.write_fusion_check(ok, detail)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("fusion reachability keepalive failed")
            await asyncio.sleep(interval)

    async def on_call(
        self,
        call_id: str,
        caller_user: str,
        display_name: str,
        from_header: str,
        event_id: str = "",
    ):
        """Callback invoked when a SIP call is answered.

        Record-first: parse the caller, build the TTS, and persist the page as a
        PENDING row. The delivery worker sends it (with retries) asynchronously.
        This is what makes a Code Blue durable across a Fusion outage or a crash
        between answering the call and delivering the page.
        """
        caller = parse_caller(from_header)
        tts = build_tts(caller)
        tts = assemble_tts(
            tts,
            play_count=self.config.tts.play_count,
            message_preamble=self.config.tts.message_preamble,
            iteration_preamble=self.config.tts.iteration_preamble,
        )

        area_name = ""
        if caller.area_number is not None:
            from .lookups import get_area_name
            area_name = get_area_name(caller.area_number)

        row_id = await self.db.create_pending_call(
            caller_id=caller.raw_user,
            display_name=caller.display_name,
            area_number=caller.area_number,
            area_name=area_name,
            room_number=caller.room_number,
            tts_string=tts,
            sip_call_id=call_id,
            is_test=1 if self._dry_run else 0,
            event_id=event_id or None,   # #15 persist upstream event id (NULL if absent)
        )

        # #5 clinical dedupe — SHADOW/DISABLED. Runs AFTER the record-first
        # insert (record-first is sacred; the insert is never gated). This is
        # NON-suppressing telemetry: it may annotate duplicate_of and log, but
        # it NEVER skips or delays delivery — the worker still delivers this
        # pending row, so a real second Code Blue for the same room is sent.
        try:
            from .lookups import get_call_purpose
            decision = await self.deduper.evaluate(
                self.db,
                caller=caller,
                purpose=get_call_purpose(caller.display_name),
                row_id=row_id,
                is_test=1 if self._dry_run else 0,
                sip_call_id=call_id,   # #5 telemetry: log the current page's Call-ID too
            )
            if decision.duplicate_of is not None:
                await self.db.record_duplicate_of(row_id, decision.duplicate_of)
                logger.info(
                    "Call %s (row %s) is a clinical duplicate of row %s "
                    "(fp=%s) — delivering anyway (SHADOW)",
                    call_id, row_id, decision.duplicate_of, decision.fingerprint)
        except Exception:
            logger.exception(
                "dedupe evaluate failed for row %s — delivering anyway", row_id)

        logger.info(f"Call {call_id} recorded PENDING (row {row_id}): tts='{tts}'")

    async def run(self):
        """Start all services and run until shutdown."""
        # Initialize components
        await self.db.initialize()
        await self.webhook.initialize()
        await self.escalator.initialize()          # #3 escalation client
        await self.webhook.start_token_refresh()   # #4 keep the token warm

        # Start the SIP listener FIRST so pages can be received. The dashboard
        # runs in its own process now (#14); it reads the heartbeat for /health.
        sip_task = asyncio.create_task(self.sip_server.start())

        # #7 stamp an initial heartbeat before /health is consulted.
        await self.db.write_heartbeat("writer")
        self._hb_task = asyncio.create_task(self._heartbeat_loop())
        # #7 start the Fusion reachability keepalive (additive, read-only, never
        # gates /health and never sends a page).
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

        # #8 tell systemd we are up and start watchdog pings BEFORE recovery.
        # A large recover() must not delay READY (watchdog restart loop); the
        # pinger then proves event-loop liveness independently of recovery.
        notify_ready()
        await self.watchdog.start()

        # #2 durable delivery: recover crash-orphaned rows, then start the worker.
        recovered = await self.worker.recover()
        if recovered:
            logger.info(f"Recovered {recovered} in-flight page(s) for redelivery")
        await self.worker.start()

        logger.info("sipgw gateway starting")

        # Wait for shutdown signal
        try:
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("Shutting down sipgw...")
            notify_stopping()             # #8 tell systemd we're stopping
            await self.watchdog.stop()
            await self.sip_server.stop()
            await self.worker.stop()      # stop delivery loop (pending rows persist)
            if self._hb_task:
                self._hb_task.cancel()
                try:
                    await self._hb_task
                except asyncio.CancelledError:
                    pass
            if self._keepalive_task:
                self._keepalive_task.cancel()
                try:
                    await self._keepalive_task
                except asyncio.CancelledError:
                    pass
            await self.webhook.close()
            await self.escalator.close()
            await self.db.close()

            # Give tasks a moment to finish
            for task in [sip_task]:
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

            logger.info("sipgw shutdown complete")

    def request_shutdown(self):
        self._shutdown_event.set()


def main():
    """CLI entry point."""
    # Load config
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    config = load_config(config_path)

    # --- §2 safety gates: dry-run marker + hard production-DB barrier ---
    from .safety import (
        effective_dry_run, assert_safe_database_path, DRY_RUN_BANNER,
    )
    dry_run = effective_dry_run(config.fusion.dry_run)

    # Configure logging with the [TEST] marker installed FIRST when in dry-run,
    # so every line (including logging's own init lines) is marked.
    setup_logging(config.logging, dry_run=dry_run)
    if dry_run:
        logger.critical(DRY_RUN_BANNER)

    # Refuse to start if dry-run/test mode would write to the production DB.
    assert_safe_database_path(config.database.path, dry_run)

    # #9 Validate configuration; refuse to start on fatal problems so a
    # misconfigured prod cannot silently fail on the first Code Blue.
    from .config import validate_config, ConfigError
    try:
        for w in validate_config(config, dry_run):
            logger.warning("config: %s", w)
    except ConfigError as e:
        logger.critical(str(e))
        raise SystemExit(2)

    # Load lookup tables
    lookups_path = os.environ.get("SIPGW_LOOKUPS", "/opt/sipgw/lookups.yaml")
    load_lookups(lookups_path)

    logger.info(f"Configuration loaded from {config_path or 'default path'}")
    logger.info(f"SIP bind: {config.sip.bind_ip}:{config.sip.bind_port}")
    logger.info("Dashboard runs as a separate process (sipgw.dashboard_app)")
    logger.info(f"Fusion scenario: {config.fusion.scenario_id}")
    logger.info(f"Call timeout: {config.sip.call_timeout_seconds}s")

    # Create and run gateway
    gateway = SIPGateway(config)

    # Handle signals
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, gateway.request_shutdown)

    try:
        loop.run_until_complete(gateway.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
