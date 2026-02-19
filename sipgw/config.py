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
    rtp_port_range_start: int = 10000
    rtp_port_range_end: int = 20000


@dataclass
class FusionConfig:
    base_url: str = "https://api.icmobile.singlewire.com/api"
    token_url: str = "https://api.icmobile.singlewire.com/api/token"
    audience: str = ""
    scenario_id: str = "YOUR_SCENARIO_ID"
    scenario_endpoint: str = "/v1/scenario-notifications"
    variable_name: str = "customTTS"
    scenario_field_id: str = ""
    client_id: str = ""
    client_secret: str = ""


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
    timezone: str = "America/New_York"
    api_debug_log: bool = True


@dataclass
class DashboardConfig:
    port: int = 8080
    bind_ip: str = "0.0.0.0"
    auto_refresh_seconds: int = 10


@dataclass
class DatabaseConfig:
    path: str = "/var/lib/sipgw/calls.db"


@dataclass
class AppConfig:
    sip: SIPConfig = field(default_factory=SIPConfig)
    fusion: FusionConfig = field(default_factory=FusionConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)


def _apply_section(target, raw_dict: dict):
    """Apply raw config dict values onto a dataclass instance."""
    for k, v in raw_dict.items():
        if hasattr(target, k):
            setattr(target, k, v)


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
            "logging": config.logging,
            "dashboard": config.dashboard,
            "database": config.database,
        }
        for section_name, target in section_map.items():
            if section_name in raw and isinstance(raw[section_name], dict):
                _apply_section(target, raw[section_name])

    return config
