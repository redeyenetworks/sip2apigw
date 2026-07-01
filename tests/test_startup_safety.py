"""Startup-safety plumbing: config -> effective_dry_run -> prod-DB barrier.

Hermetic: writes its own temp configs (never reads the gitignored staging
config.yaml) so it passes on any checkout.
"""

import textwrap

import pytest

from sipgw.config import load_config
from sipgw.safety import (
    ProdDatabaseBarrier,
    assert_safe_database_path,
    effective_dry_run,
)


def _write(tmp_path, body: str) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body))
    return str(p)


def test_dry_run_with_staging_db_starts(tmp_path):
    cfg_path = _write(tmp_path, f"""
        fusion:
          dry_run: true
        database:
          path: "{tmp_path}/calls.db"
    """)
    cfg = load_config(cfg_path)
    dry = effective_dry_run(cfg.fusion.dry_run)
    assert dry is True
    # Must NOT raise: staging path.
    assert_safe_database_path(cfg.database.path, dry)


def test_dry_run_with_prod_db_is_refused(tmp_path):
    cfg_path = _write(tmp_path, """
        fusion:
          dry_run: true
        database:
          path: "/var/lib/sipgw/calls.db"
    """)
    cfg = load_config(cfg_path)
    dry = effective_dry_run(cfg.fusion.dry_run)
    with pytest.raises(ProdDatabaseBarrier):
        assert_safe_database_path(cfg.database.path, dry)


def test_prod_db_allowed_when_not_dry_run(tmp_path):
    cfg_path = _write(tmp_path, """
        fusion:
          dry_run: false
        database:
          path: "/var/lib/sipgw/calls.db"
    """)
    cfg = load_config(cfg_path)
    dry = effective_dry_run(cfg.fusion.dry_run)
    assert dry is False
    # Production running normally uses the prod path — allowed.
    assert_safe_database_path(cfg.database.path, dry)


def test_env_forces_dry_run_and_then_prod_db_is_refused(tmp_path, monkeypatch):
    # Config says not-dry-run + prod DB (i.e. a normal prod config), but the
    # env var forces dry-run on -> the barrier must now refuse the prod DB.
    monkeypatch.setenv("SIPGW_DRY_RUN", "1")
    cfg_path = _write(tmp_path, """
        fusion:
          dry_run: false
        database:
          path: "/var/lib/sipgw/calls.db"
    """)
    cfg = load_config(cfg_path)
    dry = effective_dry_run(cfg.fusion.dry_run)
    assert dry is True
    with pytest.raises(ProdDatabaseBarrier):
        assert_safe_database_path(cfg.database.path, dry)
