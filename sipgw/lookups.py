"""Lookup tables for area names and call purpose substitutions.

Tables are loaded from lookups.yaml so they can be edited without code changes.
"""

import os
import yaml
import logging
from typing import Dict, Optional
from pathlib import Path

logger = logging.getLogger("sipgw.lookups")

DEFAULT_LOOKUPS_PATH = "/opt/sipgw/lookups.yaml"

# Module-level cache
_area_map: Dict[str, str] = {}
_purpose_map: Dict[str, str] = {}
_room_map: Dict[str, str] = {}
_default_area: str = "Unknown Area."
_default_purpose: str = "Code"
_default_room_format: str = "Room {room}."
_loaded: bool = False


def load_lookups(path: Optional[str] = None) -> None:
    """Load lookup tables from YAML file."""
    global _area_map, _purpose_map, _room_map, _default_area, _default_purpose, _default_room_format, _loaded

    lookups_path = path or os.environ.get("SIPGW_LOOKUPS", DEFAULT_LOOKUPS_PATH)

    if not os.path.exists(lookups_path):
        logger.warning(f"Lookups file not found: {lookups_path}, using empty tables")
        _loaded = True
        return

    with open(lookups_path, "r") as f:
        raw = yaml.safe_load(f) or {}

    _area_map = {str(k): v for k, v in raw.get("areas", {}).items()}
    _default_area = raw.get("default_area", "Unknown Area.")
    _purpose_map = raw.get("call_purposes", {})
    _default_purpose = raw.get("default_purpose", "Code")
    _room_map = {str(k): v for k, v in raw.get("rooms", {}).items()}
    _default_room_format = raw.get("default_room_format", "Room {room}.")
    _loaded = True

    logger.info(
        f"Loaded {len(_area_map)} area, {len(_purpose_map)} purpose, "
        f"and {len(_room_map)} room mappings"
    )


def _ensure_loaded():
    if not _loaded:
        load_lookups()


def get_area_name(area_id: str) -> str:
    """Look up a speech-ready area name by area ID string."""
    _ensure_loaded()
    return _area_map.get(area_id, _default_area)


def get_room_name(room_number: str) -> str:
    """Look up a speech-ready room name by room number string.

    If the room number has a mapping, returns the mapped name followed by a period.
    Otherwise returns the default format (e.g. "Room 01196.").
    Leading zeros from the SIP username are preserved.
    """
    _ensure_loaded()
    if room_number in _room_map:
        return _room_map[room_number] + "."
    return _default_room_format.format(room=room_number)


def get_call_purpose(display_name: str) -> str:
    """Derive call purpose from SIP display name.

    Searches for known keywords (Blue, RRT, Pink) in the display name
    and returns the corresponding substitution (Code Blue, Rapid Response Team, etc.).
    Returns default_purpose if no match or empty display name.
    """
    _ensure_loaded()

    if not display_name or not display_name.strip():
        return _default_purpose

    for keyword, substitution in _purpose_map.items():
        if keyword in display_name:
            return substitution

    return _default_purpose
