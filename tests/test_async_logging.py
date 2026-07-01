"""#6 async logging: file I/O happens off-thread via QueueHandler/QueueListener."""

import logging

from sipgw.logging_config import _add_async_handler, _ASYNC_LISTENERS, stop_async_logging


def test_record_written_off_thread_and_flushed(tmp_path):
    log_file = tmp_path / "async.log"
    real = logging.FileHandler(str(log_file))
    real.setFormatter(logging.Formatter("%(message)s"))
    real.setLevel(logging.DEBUG)

    lg = logging.getLogger("sipgw.async_test_a")
    lg.handlers = []
    lg.propagate = False
    lg.setLevel(logging.DEBUG)

    n_before = len(_ASYNC_LISTENERS)
    _add_async_handler(lg, real)
    assert len(_ASYNC_LISTENERS) == n_before + 1
    # The logger's attached handler is the fast QueueHandler, not the file.
    from logging.handlers import QueueHandler
    assert isinstance(lg.handlers[0], QueueHandler)

    lg.info("async-line-1")
    lg.warning("async-line-2")

    # Stop listeners -> flush queued records to disk.
    stop_async_logging()

    text = log_file.read_text()
    assert "async-line-1" in text
    assert "async-line-2" in text


def test_respects_handler_level(tmp_path):
    log_file = tmp_path / "async2.log"
    real = logging.FileHandler(str(log_file))
    real.setFormatter(logging.Formatter("%(message)s"))
    real.setLevel(logging.WARNING)   # DEBUG/INFO must be dropped by the listener

    lg = logging.getLogger("sipgw.async_test_b")
    lg.handlers = []
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    _add_async_handler(lg, real)

    lg.debug("should-not-appear")
    lg.error("should-appear")
    stop_async_logging()

    text = log_file.read_text()
    assert "should-appear" in text
    assert "should-not-appear" not in text
