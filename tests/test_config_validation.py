"""#9 config validation tests."""

import textwrap

import pytest

from sipgw.config import (
    AppConfig, ConfigError, FusionConfig, load_config, validate_config,
)


def _prod_ok() -> AppConfig:
    c = AppConfig()
    c.fusion = FusionConfig(
        base_url="https://api.icmobile.singlewire.com/api",
        token_url="https://api.icmobile.singlewire.com/api/token",
        audience="prov", scenario_id="scen", scenario_field_id="field",
        client_id="cid", client_secret="secret",
    )
    c.escalation.webhook_url = "https://hooks.example.com/escalation"
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

    def test_missing_escalation_warns_in_prod(self):
        c = _prod_ok()
        c.escalation.webhook_url = ""
        warnings = validate_config(c, dry_run=False)
        assert any("escalation.webhook_url" in w for w in warnings)

    def test_bad_escalation_url_is_fatal(self):
        c = _prod_ok()
        c.escalation.webhook_url = "hooks.example.com/escalation"  # no scheme
        with pytest.raises(ConfigError):
            validate_config(c, dry_run=False)


class TestKeepaliveInterval:
    """#7 health.keepalive_interval_seconds — additive; never fatal."""

    def test_default_interval_no_warning(self):
        # Default (300s) is sane and must not add a keepalive warning.
        warnings = validate_config(_prod_ok(), dry_run=False)
        assert not any("keepalive_interval_seconds" in w for w in warnings)

    def test_tiny_interval_warns_not_fatal(self):
        c = _prod_ok()
        c.health.keepalive_interval_seconds = 1.0
        warnings = validate_config(c, dry_run=False)   # must not raise
        assert any("keepalive_interval_seconds" in w for w in warnings)

    def test_keepalive_key_loaded_from_yaml(self, tmp_path):
        import textwrap
        p = tmp_path / "config.yaml"
        p.write_text(textwrap.dedent("""
            health:
              keepalive_interval_seconds: 120.0
        """))
        config = load_config(str(p))
        assert config.health.keepalive_interval_seconds == 120.0
        assert config.load_warnings == []          # known key, no typo warning


class TestImmediateByeAckTimeout:
    """#11 sip.immediate_bye_ack_timeout_seconds — additive; never fatal."""

    def test_default_no_warning(self):
        # Default (2.0s) with immediate_bye on must not warn.
        c = _prod_ok()
        c.sip.immediate_bye = True
        warnings = validate_config(c, dry_run=False)
        assert not any("immediate_bye_ack_timeout_seconds" in w for w in warnings)

    def test_nonpositive_warns_not_fatal_when_immediate_bye_on(self):
        c = _prod_ok()
        c.sip.immediate_bye = True
        c.sip.immediate_bye_ack_timeout_seconds = 0.0
        warnings = validate_config(c, dry_run=False)   # must not raise
        assert any("immediate_bye_ack_timeout_seconds" in w for w in warnings)

    def test_nonpositive_no_warning_when_immediate_bye_off(self):
        # The knob is irrelevant when immediate_bye is off, so no warning.
        c = _prod_ok()
        c.sip.immediate_bye = False
        c.sip.immediate_bye_ack_timeout_seconds = 0.0
        warnings = validate_config(c, dry_run=False)
        assert not any("immediate_bye_ack_timeout_seconds" in w for w in warnings)

    def test_key_loaded_from_yaml_no_typo_warning(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text(textwrap.dedent("""
            sip:
              immediate_bye_ack_timeout_seconds: 1.5
        """))
        config = load_config(str(p))
        assert config.sip.immediate_bye_ack_timeout_seconds == 1.5
        assert config.load_warnings == []          # known key, no typo warning


class TestFailOnFusionUnreachable:
    """#7 opt-in degrade flag — additive; default OFF; never fatal."""

    def test_default_off_no_warning(self):
        warnings = validate_config(_prod_ok(), dry_run=False)
        assert not any("fail_on_fusion_unreachable" in w for w in warnings)

    def test_on_with_probe_enabled_no_warning(self):
        c = _prod_ok()
        c.health.fail_on_fusion_unreachable = True   # keepalive default 300s
        warnings = validate_config(c, dry_run=False)
        assert not any("fail_on_fusion_unreachable" in w for w in warnings)

    def test_on_with_probe_disabled_warns_not_fatal(self):
        c = _prod_ok()
        c.health.fail_on_fusion_unreachable = True
        c.health.keepalive_interval_seconds = 0.0    # probe disabled
        warnings = validate_config(c, dry_run=False)  # must not raise
        assert any("fail_on_fusion_unreachable" in w for w in warnings)

    def test_flags_loaded_from_yaml(self, tmp_path):
        import textwrap
        p = tmp_path / "config.yaml"
        p.write_text(textwrap.dedent("""
            health:
              fail_on_fusion_unreachable: true
              fusion_unreachable_max_age_seconds: 90.0
        """))
        config = load_config(str(p))
        assert config.health.fail_on_fusion_unreachable is True
        assert config.health.fusion_unreachable_max_age_seconds == 90.0
        assert config.load_warnings == []          # known keys, no typo warning


class TestUnknownKeyWarnings:
    """#9 remaining acceptance criterion: unknown/misspelled keys are surfaced
    as non-fatal startup warnings (and still dropped, not applied)."""

    def _write(self, tmp_path, text: str) -> str:
        p = tmp_path / "config.yaml"
        p.write_text(textwrap.dedent(text))
        return str(p)

    def test_typo_section_key_warns_and_is_not_applied(self, tmp_path):
        path = self._write(tmp_path, """
            sip:
              imediate_bye: true
        """)
        config = load_config(path)
        # Unknown key still dropped -> the real field keeps its default.
        assert config.sip.immediate_bye is False
        assert any("imediate_bye" in w for w in config.load_warnings)
        # And it flows through validate_config's returned warnings.
        warnings = validate_config(config, dry_run=True)
        assert any("imediate_bye" in w for w in warnings)

    def test_unknown_top_level_section_warns(self, tmp_path):
        path = self._write(tmp_path, """
            bogus:
              foo: 1
        """)
        config = load_config(path)
        assert any("bogus" in w for w in config.load_warnings)
        warnings = validate_config(config, dry_run=True)
        assert any("bogus" in w for w in warnings)

    def test_clean_config_has_no_unknown_key_warnings(self, tmp_path):
        path = self._write(tmp_path, """
            sip:
              immediate_bye: true
              bind_port: 5060
            dedupe:
              window_seconds: 0
        """)
        config = load_config(path)
        assert config.load_warnings == []
        assert config.sip.immediate_bye is True

    def test_directly_constructed_appconfig_has_empty_load_warnings(self):
        assert AppConfig().load_warnings == []
