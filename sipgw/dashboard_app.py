"""#14 Decoupled dashboard process — SIP-to-Webhook Gateway.

The two-service split runs the FastAPI dashboard in its OWN process, separate
from the writer (``sipgw.main``). This process:

  * opens the shared SQLite DB **READ-ONLY** (``query_only=ON``) so it can never
    mutate a page or heartbeat — the writer owns all writes;
  * reads the writer's heartbeat row for ``/health`` (a plain SELECT, fine under
    query_only);
  * uses DASHBOARD-SAFE logging (``setup_dashboard_logging``) so it never
    attaches the #6 rotating file handler to the writer's shared log files
    (two processes racing midnight ``doRollover()`` would corrupt them).

Entry point:  ``python -m sipgw.dashboard_app <config.yaml>``

Bootstrap mirrors ``main.main()``: load_config, effective_dry_run, the prod-DB
barrier, and validate_config, so a misconfigured or unsafe dashboard refuses to
start exactly like the writer does.

NOTE: the writer (``sipgw.main``) keeps WRITING the heartbeat; this process only
reads it. Both services must run for a healthy system.
"""

import asyncio
import logging
import sys
from typing import Tuple

import uvicorn
from fastapi import FastAPI

from .config import load_config, validate_config, ConfigError, AppConfig
from .logging_config import setup_dashboard_logging
from .database import CallDatabase
from .dashboard import create_dashboard
from .safety import effective_dry_run, assert_safe_database_path, DRY_RUN_BANNER

logger = logging.getLogger("sipgw.dashboard_app")


async def build_dashboard(config: AppConfig,
                          dry_run: bool = False) -> Tuple[CallDatabase, FastAPI]:
    """Open the READ-ONLY database and build the FastAPI dashboard app.

    Returns ``(db, app)``. The caller owns closing ``db``. No server is started
    here so this is directly usable from tests.
    """
    db = CallDatabase(
        config.database.path,
        timezone=config.logging.timezone,
        read_only=True,
        dry_run=dry_run,
    )
    await db.initialize()
    app = create_dashboard(db, config.dashboard, config.logging, config.health)
    return db, app


async def _serve(config: AppConfig, dry_run: bool) -> None:
    db, app = await build_dashboard(config, dry_run=dry_run)
    server = uvicorn.Server(uvicorn.Config(
        app=app,
        host=config.dashboard.bind_ip,
        port=config.dashboard.port,
        log_level="warning",
        access_log=False,
    ))
    logger.info(
        "Dashboard (read-only) running on http://%s:%s",
        config.dashboard.bind_ip, config.dashboard.port)
    try:
        # uvicorn installs SIGINT/SIGTERM handlers (main thread) -> clean stop.
        await server.serve()
    finally:
        await db.close()
        logger.info("Dashboard shutdown complete")


def main():
    """CLI entry point for the decoupled dashboard process."""
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    config = load_config(config_path)

    # --- §2 safety gates: dry-run marker + hard production-DB barrier ---
    dry_run = effective_dry_run(config.fusion.dry_run)

    # DASHBOARD-SAFE logging — install the [TEST] marker FIRST in dry-run so
    # every line (including this function's own) is marked. Deliberately NOT the
    # writer's setup_logging (which would attach the rotating handler to the
    # shared writer log files).
    setup_dashboard_logging(config.logging, dry_run=dry_run)
    if dry_run:
        logger.critical(DRY_RUN_BANNER)

    # Refuse to start if dry-run/test mode would attach to the production DB —
    # the prod-DB barrier stays for the reader too.
    assert_safe_database_path(config.database.path, dry_run)

    # #9 Validate configuration; refuse to start on fatal problems.
    try:
        for w in validate_config(config, dry_run):
            logger.warning("config: %s", w)
    except ConfigError as e:
        logger.critical(str(e))
        raise SystemExit(2)

    logger.info(f"Dashboard configuration loaded from {config_path or 'default path'}")
    logger.info(f"Dashboard bind: {config.dashboard.bind_ip}:{config.dashboard.port}")
    logger.info(f"Reading DB (read-only): {config.database.path}")

    try:
        asyncio.run(_serve(config, dry_run))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
