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

logger = logging.getLogger("sipgw.main")


class SIPGateway:
    """Top-level application that coordinates all sipgw components."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.db = CallDatabase(config.database.path)
        self.webhook = FusionWebhook(config.fusion)
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

        Parses caller info, builds TTS string, triggers webhook, records to DB.
        """
        # Parse caller info
        caller = parse_caller(from_header)

        # Build TTS string
        tts = build_tts(caller)

        # Assemble with preambles and repetition
        tts = assemble_tts(
            tts,
            play_count=self.config.tts.play_count,
            message_preamble=self.config.tts.message_preamble,
            iteration_preamble=self.config.tts.iteration_preamble,
        )

        # Trigger Fusion webhook
        status_code, response_time = await self.webhook.trigger_scenario(tts)

        # Record to database
        area_name = ""
        if caller.area_number is not None:
            from .lookups import get_area_name
            area_name = get_area_name(caller.area_number)

        await self.db.record_call(
            caller_id=caller.raw_user,
            display_name=caller.display_name,
            area_number=caller.area_number,
            area_name=area_name,
            room_number=caller.room_number,
            tts_string=tts,
            fusion_status=status_code,
            response_time_ms=response_time,
        )

        logger.info(
            f"Call {call_id} processed: tts='{tts}' "
            f"fusion_status={status_code} response_time={response_time:.1f}ms"
        )

    async def run(self):
        """Start all services and run until shutdown."""
        # Initialize components
        await self.db.initialize()
        await self.webhook.initialize()

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
            await self.webhook.close()
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

    # Setup logging
    setup_logging(config.logging)

    # --- §2 safety gates: dry-run marker + hard production-DB barrier ---
    # Run BEFORE anything constructs the database or does network I/O.
    from .safety import (
        effective_dry_run, install_test_marker,
        assert_safe_database_path, DRY_RUN_BANNER,
    )
    dry_run = effective_dry_run(config.fusion.dry_run)
    if dry_run:
        install_test_marker()          # every subsequent log line is [TEST]-marked
        logger.critical(DRY_RUN_BANNER)
    # Refuse to start if dry-run/test mode would write to the production DB.
    assert_safe_database_path(config.database.path, dry_run)

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
