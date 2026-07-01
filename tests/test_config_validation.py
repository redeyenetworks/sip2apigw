"""#9 config validation tests."""

import pytest

from sipgw.config import (
    AppConfig, ConfigError, FusionConfig, validate_config,
)


def _prod_ok() -> AppConfig:
    c = AppConfig()
    c.fusion = FusionConfig(
        base_url="https://api.icmobile.singlewire.com/api",
        token_url="https://api.icmobile.singlewire.com/api/token",
        audience="prov", scenario_id="scen", scenario_field_id="field",
        client_id="cid", client_secret="secret",
    )
    return c


class TestProdRequirements:
    def test_valid_prod_config_passes(self):
        assert validate_config(_prod_ok(), dry_run=False) == []

    def test_missing_secret_is_fatal(self):
        c = _prod_ok()
        c.fusion.client_secret = ""
        with pytest.raises(ConfigError) as ei:
            validate_config(c, dry_run=False)
        assert "client_secret" in str(ei.value)

    def test_missing_field_id_is_fatal_in_prod(self):
        c = _prod_ok()
        c.fusion.scenario_field_id = ""
        with pytest.raises(ConfigError) as ei:
            validate_config(c, dry_run=False)
        assert "scenario_field_id" in str(ei.value)

    def test_dry_run_relaxes_credentials(self):
        c = AppConfig()  # empty creds, no field id
        c.fusion.dry_run = True
        # Under dry-run these are not required -> no ConfigError.
        assert isinstance(validate_config(c, dry_run=True), list)


class TestStructuralValidation:
    def test_bad_url_is_fatal(self):
        c = _prod_ok()
        c.fusion.base_url = "api.icmobile.singlewire.com"  # no scheme
        with pytest.raises(ConfigError):
            validate_config(c, dry_run=False)

    def test_bad_cidr_is_fatal(self):
        c = _prod_ok()
        c.sip.allowed_networks = ["172.16.0.0/12", "not-a-cidr"]
        with pytest.raises(ConfigError) as ei:
            validate_config(c, dry_run=False)
        assert "not-a-cidr" in str(ei.value)

    def test_rtp_range_inverted_is_fatal(self):
        c = _prod_ok()
        c.sip.rtp_port_range_start = 20000
        c.sip.rtp_port_range_end = 10000
        with pytest.raises(ConfigError):
            validate_config(c, dry_run=False)

    def test_bad_delivery_poll_is_fatal(self):
        c = _prod_ok()
        c.delivery.poll_interval_seconds = 0
        with pytest.raises(ConfigError):
            validate_config(c, dry_run=False)

    def test_empty_allowed_networks_warns_not_fatal(self):
        c = _prod_ok()
        c.sip.allowed_networks = []
        warnings = validate_config(c, dry_run=False)
        assert any("allowed_networks" in w for w in warnings)

    def test_port_out_of_range_is_fatal(self):
        c = _prod_ok()
        c.dashboard.port = 70000
        with pytest.raises(ConfigError):
            validate_config(c, dry_run=False)
