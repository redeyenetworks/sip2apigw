"""Logging configuration for sipgw.

Sets up dual logging: stdout + daily-rotating file with .tgz compression.
Rotated files are compressed and old files beyond retention are purged.
"""

import os
import gzip
import glob
import tarfile
import logging
import logging.handlers
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import atexit
import queue as _queue_mod
from logging.handlers import QueueHandler, QueueListener

from .config import LoggingConfig

# #6 async logging: QueueListeners perform file writes / rotation / .tgz
# compression on a background thread so a logging call from the event loop only
# enqueues. Flushed at interpreter exit.
_ASYNC_LISTENERS: list = []


def _add_async_handler(target_logger: logging.Logger, real_handler: logging.Handler) -> None:
    """Attach ``real_handler`` to ``target_logger`` via a QueueHandler + a
    background QueueListener, so the calling thread never blocks on disk I/O or
    a log rotation. The listener thread does the actual write/rotate/compress.
    """
    q = _queue_mod.Queue(-1)
    listener = QueueListener(q, real_handler, respect_handler_level=True)
    listener.start()
    _ASYNC_LISTENERS.append(listener)
    target_logger.addHandler(QueueHandler(q))


def stop_async_logging() -> None:
    """Flush and stop all async log listeners (idempotent)."""
    while _ASYNC_LISTENERS:
        listener = _ASYNC_LISTENERS.pop()
        try:
            listener.stop()
        except Exception:
            pass


atexit.register(stop_async_logging)


class CompressingTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """TimedRotatingFileHandler that compresses rotated files to .tgz."""

    def __init__(self, *args, retention_days: int = 90, **kwargs):
        self.retention_days = retention_days
        super().__init__(*args, **kwargs)

    def rotator(self, source: str, dest: str):
        """Compress the rotated log file into a .tgz archive."""
        tgz_path = dest + ".tgz"
        try:
            with tarfile.open(tgz_path, "w:gz") as tar:
                tar.add(source, arcname=os.path.basename(dest))
            os.remove(source)
        except Exception as e:
            logging.getLogger("sipgw.logging").error(f"Failed to compress rotated log: {e}")
            # Fall back to just renaming
            if os.path.exists(source):
                os.rename(source, dest)

    def doRollover(self):
        """Override to add cleanup of old compressed files after rotation."""
        super().doRollover()
        self._cleanup_old_files()

    def _cleanup_old_files(self):
        """Remove compressed log files older than retention_days."""
        if not self.baseFilename:
            return
        log_dir = os.path.dirname(self.baseFilename)
        base = os.path.basename(self.baseFilename)
        cutoff = datetime.now() - timedelta(days=self.retention_days)

        for path in glob.glob(os.path.join(log_dir, base + ".*")):
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(path))
                if mtime < cutoff:
                    os.remove(path)
                    logging.getLogger("sipgw.logging").info(f"Removed old log: {path}")
            except Exception as e:
                logging.getLogger("sipgw.logging").error(f"Failed to clean up {path}: {e}")


def setup_logging(config: Optional[LoggingConfig] = None, dry_run: bool = False) -> None:
    """Configure logging for the application.

    Sets up:
    - Console handler (stdout) with INFO level
    - File handler with daily rotation at midnight ET, .tgz compression, 90-day retention

    When ``dry_run`` is True, the [TEST] marker is installed on the sipgw loggers
    FIRST, so every line this function itself emits is also marked (§2b: every
    log line during testing must carry [TEST]).
    """
    if config is None:
        config = LoggingConfig()

    root_logger = logging.getLogger("sipgw")
    root_logger.setLevel(logging.DEBUG)

    if dry_run:
        # Attach to the loggers before any line is emitted or any handler added;
        # the logger-level filter marks records before they reach handlers.
        from .safety import install_test_marker
        install_test_marker()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    root_logger.addHandler(console)

    # File handler
    log_dir = Path(config.log_dir)
    if log_dir.exists():
        log_file = log_dir / "sipgw.log"
        file_handler = CompressingTimedRotatingFileHandler(
            str(log_file),
            when="midnight",
            interval=1,
            backupCount=config.retention_days,
            retention_days=config.retention_days,
            atTime=None,
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        # Set timezone-aware suffix
        file_handler.suffix = "%Y-%m-%d"
        _add_async_handler(root_logger, file_handler)   # #6 off-loop file I/O
    else:
        root_logger.warning(f"Log directory {log_dir} does not exist; file logging disabled")

    # API debug log — separate file for detailed northbound API traces
    api_logger = logging.getLogger("sipgw.api_debug")
    api_logger.propagate = False  # don't duplicate into main log

    if config.api_debug_log and log_dir.exists():
        api_log_file = log_dir / "sipgw_api_debug.log"
        api_handler = CompressingTimedRotatingFileHandler(
            str(api_log_file),
            when="midnight",
            interval=1,
            backupCount=config.retention_days,
            retention_days=config.retention_days,
            atTime=None,
        )
        api_handler.setLevel(logging.DEBUG)
        api_formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s]: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        api_handler.setFormatter(api_formatter)
        api_handler.suffix = "%Y-%m-%d"
        _add_async_handler(api_logger, api_handler)   # #6 off-loop file I/O
        root_logger.info("API debug logging enabled -> %s", api_log_file)
    elif not config.api_debug_log:
        # Add a NullHandler so logging calls don't warn
        api_logger.addHandler(logging.NullHandler())
        root_logger.info("API debug logging disabled")

    # SIP debug log — separate file for detailed SIP message traces
    sip_logger = logging.getLogger("sipgw.sip_debug")
    sip_logger.propagate = False

    if config.sip_debug_log and log_dir.exists():
        sip_log_file = log_dir / "sipgw_sip_debug.log"
        sip_handler = CompressingTimedRotatingFileHandler(
            str(sip_log_file),
            when="midnight",
            interval=1,
            backupCount=config.retention_days,
            retention_days=config.retention_days,
            atTime=None,
        )
        sip_handler.setLevel(logging.DEBUG)
        sip_formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s]: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        sip_handler.setFormatter(sip_formatter)
        sip_handler.suffix = "%Y-%m-%d"
        _add_async_handler(sip_logger, sip_handler)   # #6 off-loop file I/O
        root_logger.info("SIP debug logging enabled -> %s", sip_log_file)
    elif not config.sip_debug_log:
        sip_logger.addHandler(logging.NullHandler())
        root_logger.info("SIP debug logging disabled")

    if dry_run:
        # Second install now that all handlers exist: attaches the [TEST] filter
        # to every handler so child-logger records (sipgw.main, sipgw.webhook,
        # sipgw.delivery, ...) are marked too. The early logger-level install
        # already covers lines logged directly on the sipgw logger above.
        from .safety import install_test_marker
        install_test_marker()

    root_logger.info("Logging initialized")


def setup_dashboard_logging(config: Optional[LoggingConfig] = None,
                            dry_run: bool = False) -> None:
    """#14 DASHBOARD-SAFE logging for the decoupled dashboard process.

    The dashboard (``python -m sipgw.dashboard_app``) runs in a SEPARATE process
    from the writer (``python -m sipgw.main``). It MUST NOT attach the #6
    CompressingTimedRotatingFileHandler to the writer's shared files
    (sipgw.log / sipgw_api_debug.log / sipgw_sip_debug.log): two processes each
    owning a rotating handler on the same file would race at midnight in
    ``doRollover()`` and corrupt or lose logs.

    So this installs a console/StreamHandler (journald captures it under
    systemd) plus, optionally, the dashboard's OWN ``sipgw_dashboard.log``
    rotating file — a distinct filename the writer never touches. As in
    ``setup_logging``, the [TEST] marker is installed FIRST in dry-run so every
    line this emits is marked.
    """
    if config is None:
        config = LoggingConfig()

    root_logger = logging.getLogger("sipgw")
    root_logger.setLevel(logging.DEBUG)

    if dry_run:
        # Mark before any line is emitted (logger-level filter).
        from .safety import install_test_marker
        install_test_marker()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler (journald captures stdout/stderr under systemd).
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    root_logger.addHandler(console)

    # Optional OWN rotating file — NEVER the writer's sipgw.log. The distinct
    # filename guarantees the two processes never share a rotating handler.
    log_dir = Path(config.log_dir)
    if log_dir.exists():
        dash_log_file = log_dir / "sipgw_dashboard.log"
        dash_handler = CompressingTimedRotatingFileHandler(
            str(dash_log_file),
            when="midnight",
            interval=1,
            backupCount=config.retention_days,
            retention_days=config.retention_days,
            atTime=None,
        )
        dash_handler.setLevel(logging.DEBUG)
        dash_handler.setFormatter(formatter)
        dash_handler.suffix = "%Y-%m-%d"
        _add_async_handler(root_logger, dash_handler)   # #6 off-loop file I/O
    else:
        root_logger.warning(
            f"Log directory {log_dir} does not exist; dashboard file logging disabled")

    if dry_run:
        # Second install now that the handlers exist so propagated child-logger
        # records are marked at the handler level too.
        from .safety import install_test_marker
        install_test_marker()

    root_logger.info("Dashboard logging initialized")
