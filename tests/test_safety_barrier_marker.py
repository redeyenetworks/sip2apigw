"""§2b tests: [TEST] log marker and the production-DB hard barrier."""

import io
import logging

import pytest

from sipgw.safety import (
    ProdDatabaseBarrier,
    PROD_DB_PATH,
    TestMarkerFilter,
    assert_safe_database_path,
    install_test_marker,
)


def _isolated_logger(name: str):
    """A logger with a single StringIO handler and no propagation."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger(name)
    lg.handlers = [handler]
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    return lg, buf


class TestMarker:
    def test_prefixes_message(self):
        lg, buf = _isolated_logger("sipgw.mark_a")
        install_test_marker(lg.name)
        lg.info("hello %s", "world")
        assert buf.getvalue().strip() == "[TEST] hello world"

    def test_marks_child_via_parent_handler(self):
        lg, buf = _isolated_logger("sipgw.mark_b")
        install_test_marker(lg.name)
        logging.getLogger("sipgw.mark_b.child").info("nested")
        assert buf.getvalue().strip() == "[TEST] nested"

    def test_no_double_prefix(self):
        # Filter on both the logger and its handler must not double-mark.
        lg, buf = _isolated_logger("sipgw.mark_c")
        install_test_marker(lg.name)
        lg.warning("once")
        out = buf.getvalue().strip()
        assert out == "[TEST] once"
        assert out.count("[TEST]") == 1

    def test_every_line_of_multiline_record_is_marked(self):
        # SIP/API dumps are multi-line; no continuation line may be unmarked.
        lg, buf = _isolated_logger("sipgw.mark_ml")
        install_test_marker(lg.name)
        lg.info(">>> SEND to %s (%s)\n%s", "127.0.0.1:5062", "udp",
                "INVITE sip:gw SIP/2.0\r\nVia: x\r\nFrom: y")
        lines = [ln for ln in buf.getvalue().split("\n") if ln.strip()]
        assert lines, "expected output"
        assert all(ln.startswith("[TEST] ") for ln in lines), lines

    def test_install_is_idempotent(self):
        lg, buf = _isolated_logger("sipgw.mark_d")
        install_test_marker(lg.name)
        install_test_marker(lg.name)   # second call must not add a 2nd filter
        marker_filters = [f for f in lg.handlers[0].filters
                          if isinstance(f, TestMarkerFilter)]
        assert len(marker_filters) == 1
        lg.info("x")
        assert buf.getvalue().strip() == "[TEST] x"


class TestProdDbBarrier:
    def test_blocks_prod_path_in_dry_run(self):
        with pytest.raises(ProdDatabaseBarrier):
            assert_safe_database_path(PROD_DB_PATH, dry_run=True)

    def test_blocks_prod_path_relative_and_dotslash(self):
        with pytest.raises(ProdDatabaseBarrier):
            assert_safe_database_path("/var/lib/sipgw/./calls.db", dry_run=True)

    def test_allows_staging_path_in_dry_run(self, tmp_path):
        # Must not raise for a staging-only path.
        assert_safe_database_path(str(tmp_path / "calls.db"), dry_run=True)

    def test_allows_prod_path_when_not_dry_run(self):
        # Production running normally uses the prod path — allowed.
        assert_safe_database_path(PROD_DB_PATH, dry_run=False)
