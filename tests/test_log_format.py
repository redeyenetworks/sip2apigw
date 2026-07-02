"""#12 log-timestamp format: canonical UTC RFC3339 millis-Z.

Asserts the ISO8601Formatter renders `asctime` as `...THH:MM:SS.mmmZ` (UTC, with
the literal `Z` and exactly 3 fractional digits) regardless of host timezone /
DST, and that every configured log formatter uses it end-to-end.
"""

import logging
import logging.handlers
import re

from sipgw.logging_config import ISO8601Formatter, setup_logging
from sipgw.config import LoggingConfig

# Exact acceptance-criteria pattern from the issue body.
STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _record(epoch: float) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="sipgw.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    rec.created = epoch
    return rec


def test_formatter_emits_exact_utc_z_stamp():
    fmt = ISO8601Formatter(fmt="%(asctime)s [%(levelname)s]: %(message)s")
    # 2021-01-01T00:00:00.000Z  (winter / no DST)
    stamp = fmt.formatTime(_record(1609459200.0))
    assert stamp == "2021-01-01T00:00:00.000Z"
    assert STAMP_RE.match(stamp)


def test_formatter_millis_are_three_digits():
    fmt = ISO8601Formatter()
    # 7 ms past the epoch second -> must render as .007 (zero-padded).
    stamp = fmt.formatTime(_record(1609459200.007))
    assert stamp == "2021-01-01T00:00:00.007Z"
    assert STAMP_RE.match(stamp)


def test_formatter_dst_invariance_utc():
    """A winter and a summer epoch both render as UTC-Z; the host DST offset
    must not appear in either stamp."""
    fmt = ISO8601Formatter()
    winter = fmt.formatTime(_record(1609459200.0))       # 2021-01-01 UTC
    summer = fmt.formatTime(_record(1625097600.0))       # 2021-07-01 UTC
    assert winter == "2021-01-01T00:00:00.000Z"
    assert summer == "2021-07-01T00:00:00.000Z"
    for stamp in (winter, summer):
        assert stamp.endswith("Z")
        assert STAMP_RE.match(stamp)


def test_full_format_line_uses_utc_stamp():
    fmt = ISO8601Formatter(fmt="%(asctime)s [%(levelname)s]: %(message)s")
    line = fmt.format(_record(1609459200.0))
    assert line == "2021-01-01T00:00:00.000Z [INFO]: hello"


def test_configured_handlers_all_use_iso8601formatter(tmp_path):
    """setup_logging must apply ISO8601Formatter to every stream: the main
    console + file, plus the api_debug and sip_debug files. #6 async wraps the
    file handlers in a QueueHandler + background QueueListener, so the real
    (formatter-bearing) handlers are reached via the listeners."""
    import sipgw.logging_config as lc

    for logger_name in ("sipgw", "sipgw.api_debug", "sipgw.sip_debug"):
        lg = logging.getLogger(logger_name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
    lc.stop_async_logging()

    cfg = LoggingConfig(
        log_dir=str(tmp_path),
        api_debug_log=True,
        sip_debug_log=True,
    )
    setup_logging(cfg)

    seen = 0
    # Direct (non-async) handlers, e.g. the console StreamHandler.
    for logger_name in ("sipgw", "sipgw.api_debug", "sipgw.sip_debug"):
        for h in logging.getLogger(logger_name).handlers:
            if isinstance(h, (logging.NullHandler, logging.handlers.QueueHandler)):
                continue
            if h.formatter is not None:
                assert isinstance(h.formatter, ISO8601Formatter)
                seen += 1
    # Async file handlers live behind the QueueListeners.
    for listener in lc._ASYNC_LISTENERS:
        for real in listener.handlers:
            assert isinstance(real.formatter, ISO8601Formatter), (
                f"async handler {real!r} uses {real.formatter!r}"
            )
            seen += 1

    # console + 3 async files (main, api_debug, sip_debug) = 4 streams.
    assert seen >= 4
