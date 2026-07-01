"""Main entry point for sipgw — SIP-to-Webhook Gateway.

Wires together all components and runs the SIP server + dashboard concurrently.
"""

import asyncio
import logging
import signal
import sys
import os
import uvicorn

from .config import load_config, AppConfig
from .lookups import load_lookups
from .logging_config import setup_logging
from .database import CallDatabase
from .webhook import FusionWebhook
from .parser import parse_caller
from .tts_builder import build_tts, assemble_tts
from .sip_server import SIPServer
from .dashboard import create_dashboard
from .delivery import DeliveryWorker
from .escalation import Escalator
from .safety import effective_dry_run

logger = logging.getLogger("sipgw.main")


class SIPGateway:
    """Top-level application that coordinates all sipgw components."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._dry_run = effective_dry_run(config.fusion.dry_run)
        self.db = CallDatabase(config.database.path, timezone=config.logging.timezone)
        self.webhook = FusionWebhook(config.fusion)
        # #3 escalation, shares the no-send guard in dry-run.
        self.escalator = Escalator(config.escalation, dry_run=self._dry_run)
        # #2 durable delivery escalates via the Escalator on failed/expired.
        self.worker = DeliveryWorker(self.db, self.webhook, config.delivery,
                                     on_escalate=self.escalator.escalate)
        self.sip_server = SIPServer(config=config, on_call=self.on_call)
        self.dashboard = create_dashboard(self.db, config.dashboard, config.logging)
        self._shutdown_event = asyncio.Event()

    async def on_call(
        self,
        call_id: str,
        caller_user: str,
        display_name: str,
        from_header: str,
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
        )

        logger.info(f"Call {call_id} recorded PENDING (row {row_id}): tts='{tts}'")

    async def run(self):
        """Start all services and run until shutdown."""
        # Initialize components
        await self.db.initialize()
        await self.webhook.initialize()
        await self.escalator.initialize()          # #3 escalation client
        await self.webhook.start_token_refresh()   # #4 keep the token warm

        # #2 durable delivery: recover crash-orphaned rows, then start the worker.
        # (When #8 watchdog lands, systemd READY=1 must be sent BEFORE recover so
        # a large recovery cannot trip the watchdog into a restart loop.)
        recovered = await self.worker.recover()
        if recovered:
            logger.info(f"Recovered {recovered} in-flight page(s) for redelivery")
        await self.worker.start()

        logger.info("sipgw gateway starting")

        # Run SIP server and dashboard concurrently
        sip_task = asyncio.create_task(self.sip_server.start())

        # Run uvicorn in the same event loop
        uvicorn_config = uvicorn.Config(
            app=self.dashboard,
            host=self.config.dashboard.bind_ip,
            port=self.config.dashboard.port,
            log_level="warning",
            access_log=False,
        )
        uvicorn_server = uvicorn.Server(uvicorn_config)
        dashboard_task = asyncio.create_task(uvicorn_server.serve())

        logger.info(
            f"Dashboard running on http://{self.config.dashboard.bind_ip}:{self.config.dashboard.port}"
        )

        # Wait for shutdown signal
        try:
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("Shutting down sipgw...")
            await self.sip_server.stop()
            uvicorn_server.should_exit = True
            await self.worker.stop()      # stop delivery loop (pending rows persist)
            await self.webhook.close()
            await self.escalator.close()
            await self.db.close()

            # Give tasks a moment to finish
            for task in [sip_task, dashboard_task]:
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
    logger.info(f"Dashboard port: {config.dashboard.port}")
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
