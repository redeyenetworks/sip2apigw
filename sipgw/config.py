"""Configuration loader for sipgw.

Loads settings from config.yaml with sensible defaults for all values.
"""

import os
import yaml
from dataclasses import dataclass, field
from typing import List, Optional
from pathlib import Path

DEFAULT_CONFIG_PATH = "/opt/sipgw/config.yaml"


@dataclass
class SIPConfig:
    bind_ip: str = "0.0.0.0"
    bind_port: int = 5060
    allowed_networks: List[str] = field(default_factory=lambda: ["172.16.0.0/12"])
    call_timeout_seconds: int = 600
    immediate_bye: bool = False
    rtp_port_range_start: int = 10000
    rtp_port_range_end: int = 20000


@dataclass
class FusionConfig:
    base_url: str = "https://api.icmobile.singlewire.com/api"
    token_url: str = "https://api.icmobile.singlewire.com/api/token"
    audience: str = ""
    scenario_id: str = ""
    scenario_endpoint: str = "/v1/scenario-notifications"
    variable_name: str = "customTTS"
    scenario_field_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    # Proactively refresh the OAuth2 token this many seconds before it expires,
    # so a page never blocks on a token round-trip (#4 background refresh).
    token_refresh_margin_seconds: int = 300
    # When True (or when env SIPGW_DRY_RUN=1), the webhook HTTP client is built
    # with the NoSendGuardTransport so NO outbound notification can reach a real
    # host. Env can only ENABLE this, never disable it. See sipgw/safety.py.
    dry_run: bool = False


@dataclass
class TTSConfig:
    play_count: int = 3
    message_preamble: str = "Attention! "
    iteration_preamble: str = "Attention! "


@dataclass
class LoggingConfig:
    log_dir: str = "/var/log/sipgw"
    retention_days: int = 90
    rotation_time: str = "midnight"
    timezone: str = ""   # #12 display/day-boundary zone; "" = read the host's local tz
    api_debug_log: bool = True
    sip_debug_log: bool = True


@dataclass
class DashboardConfig:
    port: int = 8080
    bind_ip: str = "0.0.0.0"
    auto_refresh_seconds: int = 30
    page_size: int = 20


@dataclass
class DatabaseConfig:
    path: str = "/var/lib/sipgw/calls.db"


@dataclass
class DeliveryConfig:
    """#2 durable-delivery retry worker tuning."""
    max_attempts: int = 6            # attempts before a page is marked 'failed' + escalated
    base_backoff_seconds: float = 2.0
    max_backoff_seconds: float = 60.0
    max_age_seconds: float = 900.0   # undelivered longer than this -> 'expired' + escalate
    poll_interval_seconds: float = 1.0
    batch_size: int = 20


@dataclass
class EscalationConfig:
    """#3 escalation: alert a human channel when a page cannot be delivered.

    webhook_url points at a Teams/Slack/PagerDuty/NOC endpoint. Empty disables
    escalation (failures are still logged at ERROR). In dry-run the escalation
    client carries the §2a no-send guard, so this URL is blocked in testing.
    """
    webhook_url: str = ""
    timeout_seconds: float = 10.0


@dataclass
class HealthConfig:
    """#7 liveness heartbeat + /health staleness + Fusion reachability keepalive.

    ``keepalive_interval_seconds`` paces the writer-side, READ-ONLY Fusion
    reachability probe (a bounded GET of the scenario — never a page). Its result
    is stamped to the DB and surfaced in /health as INFORMATIONAL fields only; it
    NEVER changes the /health status code (still heartbeat-driven).
    """
    heartbeat_interval_seconds: float = 10.0
    stale_after_seconds: float = 30.0
    keepalive_interval_seconds: float = 300.0
    # #7 OPT-IN degraded /health signal for a Fusion-unreachable probe. Default
    # OFF: /health stays byte-for-byte heartbeat-only (fusion result is purely
    # informational). When True, a PRESENT + FRESH ok=False probe makes /health
    # return 503 status='fusion-unreachable' — this intentionally lets a Fusion
    # blip 503 the node (and, if wired to an LB/monitor, pull/restart it), so it
    # is the operator's explicit opt-in. None (never stamped / older writer) and
    # STALE checks are treated as unknown and NEVER degrade (fail-safe).
    fail_on_fusion_unreachable: bool = False
    # Freshness bound for the degrade above. 0.0 = auto: derive from the probe
    # cadence (keepalive_interval * 2 + stale_after) so a normally-aged check is
    # never wrongly read as stale. Only a check newer than this can degrade.
    fusion_unreachable_max_age_seconds: float = 0.0
    # inbound-liveness (sibling of #7, for the INBOUND/Rauland direction). The
    # writer flushes the last inbound-SIP time to the DB every
    # inbound_flush_interval_seconds; the dashboard shows it amber once older than
    # inbound_stale_after_seconds (INFORMATIONAL only — never gates /health).
    # inbound_escalate_after_seconds > 0 opts INTO a once-per-episode webhook alert
    # via the #3 Escalator; 0 = OFF (default). Rauland only sends on real events
    # (no keepalives) and ~24% of days have zero calls, so the escalation floor is
    # generous: > the observed ~4.27-day max quiet gap.
    inbound_flush_interval_seconds: float = 30.0
    inbound_stale_after_seconds: float = 432000.0      # 5 days (amber threshold)
    inbound_escalate_after_seconds: float = 0.0        # 0 = OFF (opt-in)


@dataclass
class DedupeConfig:
    """#5 clinical dedupe — ships SHADOW/DISABLED.

    A real second Code Blue for the same room must NEVER be dropped, so this is
    inert by default: ``enforce`` False (never suppresses) and ``window_seconds``
    0 (the shadow lookup never even runs). Enforcement requires clinical
    sign-off and is FORBIDDEN today — validate_config makes ``enforce=True``
    fatal. ``window_seconds`` > 0 is a test-only override that turns the shadow
    'WOULD suppress' telemetry on; delivery still always proceeds.
    """
    enforce: bool = False
    window_seconds: int = 0
    match_bed: bool = True
    match_purpose: bool = True


@dataclass
class AppConfig:
    sip: SIPConfig = field(default_factory=SIPConfig)
    fusion: FusionConfig = field(default_factory=FusionConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    escalation: EscalationConfig = field(default_factory=EscalationConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    dedupe: DedupeConfig = field(default_factory=DedupeConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    # #9 Non-fatal warnings collected while loading (unknown/misspelled keys and
    # unknown top-level sections). Not a config section; seeded into
    # validate_config's returned warnings so both entry points log them.
    load_warnings: List[str] = field(default_factory=list)


def _apply_section(target, raw_dict: dict, section_name: str, warnings: List[str]):
    """Apply raw config dict values onto a dataclass instance.

    Unknown keys are NOT applied (existing behavior). #9: instead of silently
    dropping them, record a non-fatal warning naming the offending key.
    """
    for k, v in raw_dict.items():
        if hasattr(target, k):
            setattr(target, k, v)
        else:
            warnings.append(
                f"unknown key '{section_name}.{k}' ignored (typo?)")


def load_config(path: Optional[str] = None) -> AppConfig:
    """Load configuration from YAML file.

    Resolution order: explicit path > SIPGW_CONFIG env var > default path.
    Missing file or missing keys silently fall back to defaults.
    """
    config_path = path or os.environ.get("SIPGW_CONFIG", DEFAULT_CONFIG_PATH)
    config = AppConfig()

    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            raw = yaml.safe_load(f) or {}

        section_map = {
            "sip": config.sip,
            "fusion": config.fusion,
            "tts": config.tts,
            "delivery": config.delivery,
            "escalation": config.escalation,
            "health": config.health,
            "dedupe": config.dedupe,
            "logging": config.logging,
            "dashboard": config.dashboard,
            "database": config.database,
        }
        warnings: List[str] = []
        for section_name, target in section_map.items():
            if section_name in raw and isinstance(raw[section_name], dict):
                _apply_section(target, raw[section_name], section_name, warnings)

        # #9 Unknown top-level sections (typo'd or stray) are dropped too, but
        # surfaced as warnings rather than swallowed silently.
        for key in raw:
            if key not in section_map:
                warnings.append(
                    f"unknown config section '{key}' ignored (typo?)")

        config.load_warnings = warnings

    return config


class ConfigError(Exception):
    """Raised for a configuration that must not start the service."""


def validate_config(config: AppConfig, dry_run: bool) -> List[str]:
    """Validate config at startup. Raises ConfigError on fatal problems;
    returns a list of non-fatal warning strings.

    In production (dry_run off) the Fusion credentials, scenario id, and a
    PRESET scenario_field_id are required — so the first real Code Blue does not
    fail auth or trigger a live field-id lookup. In dry-run these are relaxed.
    """
    import ipaddress

    errors: List[str] = []
    # #9 Seed with any unknown-key/section warnings collected during load so
    # both entry points log them via their existing warning loop.
    warnings: List[str] = list(getattr(config, "load_warnings", []))
    f = config.fusion

    for name, val in (("fusion.base_url", f.base_url), ("fusion.token_url", f.token_url)):
        if not val or not str(val).startswith(("http://", "https://")):
            errors.append(f"{name} must be an http(s) URL (got {val!r})")

    if not dry_run:
        for name, val in (("fusion.client_id", f.client_id),
                          ("fusion.client_secret", f.client_secret),
                          ("fusion.audience", f.audience),
                          ("fusion.scenario_id", f.scenario_id)):
            if not val:
                errors.append(f"{name} is required in production (dry_run off)")
        if not f.scenario_field_id:
            errors.append(
                "fusion.scenario_field_id must be preset in production so the first "
                "real page does not trigger a live field-id lookup")

    s = config.sip
    if not (1 <= s.bind_port <= 65535):
        errors.append(f"sip.bind_port out of range: {s.bind_port}")
    if s.rtp_port_range_start >= s.rtp_port_range_end:
        errors.append(
            f"sip.rtp_port_range_start ({s.rtp_port_range_start}) must be < "
            f"rtp_port_range_end ({s.rtp_port_range_end})")
    if s.call_timeout_seconds <= 0:
        warnings.append(f"sip.call_timeout_seconds is {s.call_timeout_seconds} (<=0)")
    if not s.allowed_networks:
        warnings.append("sip.allowed_networks is empty — all SIP sources will be rejected")
    for net in s.allowed_networks:
        try:
            ipaddress.ip_network(net, strict=False)
        except ValueError as e:
            errors.append(f"sip.allowed_networks entry {net!r} is not a valid CIDR: {e}")

    d = config.delivery
    if d.max_attempts < 1:
        errors.append(f"delivery.max_attempts must be >= 1 (got {d.max_attempts})")
    if d.poll_interval_seconds <= 0:
        errors.append(f"delivery.poll_interval_seconds must be > 0 (got {d.poll_interval_seconds})")
    if d.max_age_seconds <= 0:
        warnings.append(f"delivery.max_age_seconds is {d.max_age_seconds} (<=0)")

    esc = config.escalation
    if esc.webhook_url and not str(esc.webhook_url).startswith(("http://", "https://")):
        errors.append(f"escalation.webhook_url must be an http(s) URL (got {esc.webhook_url!r})")
    if not dry_run and not esc.webhook_url:
        warnings.append("escalation.webhook_url is not set — failed/expired pages "
                        "will be logged but not escalated to a human channel")

    # #5 clinical dedupe enforcement is not approved for production use — a
    # suppressed page is a missed Code Blue. It requires clinical sign-off and
    # is forbidden today in ALL modes (dry-run included). Shadow-only.
    if config.dedupe.enforce:
        errors.append(
            "dedupe.enforce must be False — clinical dedupe SUPPRESSION is not "
            "approved (requires clinical sign-off); ships SHADOW only")

    if not (1 <= config.dashboard.port <= 65535):
        errors.append(f"dashboard.port out of range: {config.dashboard.port}")
    if not config.database.path:
        errors.append("database.path is required")

    # #7 Fusion reachability keepalive cadence. Purely informational telemetry,
    # so an odd value is never fatal — but a tiny interval would hammer Fusion
    # with reachability GETs, and <=0 would busy-spin the probe loop.
    if config.health.keepalive_interval_seconds < 30.0:
        warnings.append(
            f"health.keepalive_interval_seconds is "
            f"{config.health.keepalive_interval_seconds} (<30s) — the Fusion "
            f"reachability probe may hammer Fusion; consider >= 60s")

    # #7 opt-in Fusion-unreachable degrade. Non-fatal foot-gun guard: if the flag
    # is enabled but the keepalive probe is disabled (<=0), no FRESH ok=False
    # check is ever stamped, so the degrade can never fire — the operator has a
    # false sense of protection. Warn (never raise; dedupe #5 stays the sole
    # enforcement-fatal).
    if config.health.fail_on_fusion_unreachable and \
            config.health.keepalive_interval_seconds <= 0:
        warnings.append(
            "health.fail_on_fusion_unreachable is True but "
            "health.keepalive_interval_seconds <= 0 disables the reachability "
            "probe — no fresh check will ever be stamped, so /health can never "
            "signal 'fusion-unreachable'. Enable the keepalive or clear the flag.")

    # inbound-liveness silence escalation is OPT-IN (0 = OFF). If enabled, warn
    # when the threshold sits below the historical max quiet gap (~4.27 days /
    # 368,837 s over 99 days of real traffic) — a quiet stretch is NORMAL for
    # Rauland, so a too-low threshold would false-alarm on ordinary idle periods.
    _INBOUND_QUIET_FLOOR = 432000.0   # 5 days, just above the observed max gap
    if 0 < config.health.inbound_escalate_after_seconds < _INBOUND_QUIET_FLOOR:
        warnings.append(
            f"health.inbound_escalate_after_seconds is "
            f"{config.health.inbound_escalate_after_seconds} (< {_INBOUND_QUIET_FLOOR:.0f}s "
            f"~5d) — below the observed ~4.27-day max quiet gap; a normal idle "
            f"stretch could false-alarm. Consider >= {_INBOUND_QUIET_FLOOR:.0f}s.")

    if errors:
        raise ConfigError(
            "Invalid configuration:\n  - " + "\n  - ".join(errors))
    return warnings
