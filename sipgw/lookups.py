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
_area_room_map: Dict[str, str] = {}  # "area*room" -> name
_default_area: str = "Unknown Area."
_default_purpose: str = "Code"
_default_room_format: str = "Room {room}."
_loaded: bool = False
_lookups_path: str = ""
_lookups_mtime: float = 0.0


def load_lookups(path: Optional[str] = None) -> None:
    """Load lookup tables from YAML file.

    Tracks the file path and modification time so lookups can be
    auto-reloaded when the file changes (no service restart needed).
    """
    global _area_map, _purpose_map, _room_map, _area_room_map
    global _default_area, _default_purpose, _default_room_format
    global _loaded, _lookups_path, _lookups_mtime

    lookups_path = path or _lookups_path or os.environ.get("SIPGW_LOOKUPS", DEFAULT_LOOKUPS_PATH)
    _lookups_path = lookups_path

    if not os.path.exists(lookups_path):
        logger.warning(f"Lookups file not found: {lookups_path}, using empty tables")
        _loaded = True
        return

    try:
        with open(lookups_path, "r") as f:
            raw = yaml.safe_load(f) or {}

        _area_map = {str(k): v for k, v in raw.get("areas", {}).items()}
        _default_area = raw.get("default_area", "Unknown Area.")
        _purpose_map = raw.get("call_purposes", {})
        _default_purpose = raw.get("default_purpose", "Code")
        _room_map = {str(k): v for k, v in raw.get("rooms", {}).items()}
        _area_room_map = {str(k): v for k, v in raw.get("area_rooms", {}).items()}
        _default_room_format = raw.get("default_room_format", "Room {room}.")
        _lookups_mtime = os.path.getmtime(lookups_path)
        _loaded = True

        logger.info(
            f"Loaded {len(_area_map)} area, {len(_purpose_map)} purpose, "
            f"{len(_room_map)} room, and {len(_area_room_map)} area+room mappings "
            f"from {lookups_path}"
        )
    except Exception:
        logger.exception(f"Failed to load lookups from {lookups_path}, keeping previous data")
        _loaded = True  # keep serving with whatever was loaded before


def _ensure_loaded():
    """Load lookups if not yet loaded, or reload if the file has changed."""
    if not _loaded:
        load_lookups()
        return

    # Check if file has been modified since last load
    if _lookups_path:
        try:
            current_mtime = os.path.getmtime(_lookups_path)
            if current_mtime != _lookups_mtime:
                logger.info(f"lookups.yaml changed on disk, reloading...")
                load_lookups()
        except OSError:
            pass  # file may be mid-write, skip this check


def get_area_name(area_id: str) -> str:
    """Look up a speech-ready area name by area ID string."""
    _ensure_loaded()
    return _area_map.get(area_id, _default_area)


def get_room_name(room_number: str, area_number: Optional[str] = None) -> str:
    """Look up a speech-ready room name.

    Lookup priority:
      1. area_rooms: "area*room" combo override (e.g. "797*2201" -> "Prepost 1")
      2. rooms: room-only mapping (e.g. "1049" -> "Ablation")
      3. default_room_format: "Room {room}."

    This handles duplicate room numbers across areas — the same room number
    can have different names depending on which area it belongs to.
    """
    _ensure_loaded()

    # 1. Check area+room combo override
    if area_number is not None:
        combo_key = f"{area_number}*{room_number}"
        if combo_key in _area_room_map:
            return _area_room_map[combo_key] + "."

    # 2. Check room-only mapping
    if room_number in _room_map:
        return _room_map[room_number] + "."

    # 3. Default format
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
