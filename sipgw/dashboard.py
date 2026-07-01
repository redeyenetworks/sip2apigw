"""FastAPI dashboard for sipgw.

Provides a web UI showing call history with pagination, auto-refresh toggle,
log viewers, and health endpoint. No authentication required.
"""

import os
import csv
import io
import time
import logging
import yaml
from pathlib import Path
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse, StreamingResponse
from typing import Optional

from .database import CallDatabase, display_local
from .config import DashboardConfig, LoggingConfig

logger = logging.getLogger("sipgw.dashboard")

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>sipgw Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #e0e0e0;
            padding: 20px;
        }
        h1 { color: #00d4ff; margin-bottom: 5px; font-size: 1.5rem; }
        .header-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .subtitle { color: #888; font-size: 0.85rem; }
        .controls {
            display: flex;
            gap: 12px;
            align-items: center;
            font-size: 0.8rem;
        }
        .controls label { color: #aaa; }
        .controls select, .controls input[type=checkbox] { cursor: pointer; }
        .controls select {
            background: #16213e;
            color: #e0e0e0;
            border: 1px solid #0f3460;
            border-radius: 4px;
            padding: 3px 6px;
            font-size: 0.8rem;
        }
        .view-toggle {
            background: #16213e;
            color: #4fc3f7;
            border: 1px solid #0f3460;
            border-radius: 4px;
            padding: 3px 8px;
            text-decoration: none;
        }
        .view-toggle:hover { background: #0f3460; color: #fff; }
        .refresh-indicator {
            color: #4caf50;
            font-size: 0.75rem;
            margin-left: 4px;
        }
        .refresh-indicator.off { color: #666; }
        .stats {
            display: flex;
            gap: 20px;
            margin-bottom: 20px;
        }
        .stat-card {
            background: #16213e;
            border: 1px solid #0f3460;
            border-radius: 8px;
            padding: 15px 20px;
            min-width: 150px;
        }
        .stat-card .label { color: #888; font-size: 0.8rem; }
        .stat-card .value { color: #00d4ff; font-size: 1.5rem; font-weight: bold; }
        table {
            width: 100%;
            border-collapse: collapse;
            background: #16213e;
            border-radius: 8px;
            overflow: hidden;
        }
        thead { background: #0f3460; }
        th {
            padding: 12px 15px;
            text-align: left;
            font-weight: 600;
            color: #00d4ff;
            font-size: 0.85rem;
        }
        td {
            padding: 10px 15px;
            border-bottom: 1px solid #1a1a3e;
            font-size: 0.85rem;
        }
        tr:hover { background: #1e2a4a; }
        .status-ok { color: #4caf50; }
        .status-err { color: #f44336; }
        .status-pending { color: #ff9800; }
        .tts-col { max-width: 350px; word-wrap: break-word; }
        .empty-msg {
            text-align: center;
            padding: 40px;
            color: #666;
        }
        .pagination {
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 8px;
            margin-top: 15px;
            font-size: 0.85rem;
        }
        .pagination a, .pagination span {
            display: inline-block;
            padding: 6px 12px;
            border-radius: 4px;
            text-decoration: none;
        }
        .pagination a {
            background: #16213e;
            color: #00d4ff;
            border: 1px solid #0f3460;
        }
        .pagination a:hover { background: #0f3460; }
        .pagination .current {
            background: #0f3460;
            color: #fff;
            border: 1px solid #00d4ff;
        }
        .pagination .disabled {
            color: #444;
            border: 1px solid #2a2a3e;
            background: #16213e;
        }
        .log-panel { position: relative; margin-top: 10px; }
        .log-panel pre { margin: 0; }
        .copy-btn {
            position: absolute;
            top: 8px;
            right: 8px;
            background: #2d333b;
            color: #adbac7;
            border: 1px solid #444c56;
            border-radius: 4px;
            padding: 4px 10px;
            font-size: 0.72rem;
            cursor: pointer;
            z-index: 1;
            transition: background 0.15s, color 0.15s;
        }
        .copy-btn:hover { background: #347d39; color: #fff; border-color: #347d39; }
        .copy-btn.copied { background: #347d39; color: #fff; border-color: #347d39; }
    </style>
</head>
<body>
    <div>
        <h1>sipgw Dashboard</h1>
    </div>
    <div class="header-row">
        <div class="subtitle">
            Showing {{ calls|length }} of {{ total_calls }} calls (today)
            &bull; Page {{ page }} of {{ total_pages }}
        </div>
        <div class="controls">
            <a class="view-toggle" href="?view={% if view == 'advanced' %}summary{% else %}advanced{% endif %}&amp;page={{ page }}&amp;auto={{ '1' if auto_refresh else '0' }}&amp;refresh={{ refresh_seconds }}">{% if view == 'advanced' %}Summary view{% else %}Advanced view{% endif %}</a>
            <a class="view-toggle" href="/export.csv">Export CSV</a>
            <label>
                <input type="checkbox" id="autoRefresh" {% if auto_refresh %}checked{% endif %}>
                Auto-refresh
            </label>
            <select id="refreshInterval">
                {% for val in [10, 30, 60, 120, 300] %}
                <option value="{{ val }}" {% if refresh_seconds == val %}selected{% endif %}>{{ val }}s</option>
                {% endfor %}
            </select>
            <span id="refreshStatus" class="refresh-indicator {% if not auto_refresh %}off{% endif %}">
                {% if auto_refresh %}&#9679; ON{% else %}&#9675; OFF{% endif %}
            </span>
        </div>
    </div>

    <div class="stats">
        <div class="stat-card">
            <div class="label">Today's Calls</div>
            <div class="value">{{ total_calls }}</div>
        </div>
        <div class="stat-card">
            <div class="label">Successful</div>
            <div class="value" style="color: #4caf50;">{{ success_calls }}</div>
        </div>
        <div class="stat-card">
            <div class="label">Failed</div>
            <div class="value" style="color: #f44336;">{{ failed_calls }}</div>
        </div>
        <div class="stat-card">
            <div class="label">Pending</div>
            <div class="value" style="color: #ff9800;">{{ pending_calls }}</div>
        </div>
    </div>

    <div style="margin-bottom: 15px;">
        <button id="verifyBtn" onclick="verifyLookups()" style="background: #16213e; color: #4fc3f7; border: 1px solid #0f3460; border-radius: 6px; padding: 8px 16px; font-size: 0.85rem; cursor: pointer; transition: background 0.15s;">Verify lookups.yaml</button>
        <span id="verifyStatus" style="margin-left: 10px; font-size: 0.85rem;"></span>
    </div>
    <div id="verifyResult" style="display: none; margin-bottom: 20px;"></div>

    <table>
        <thead>
            <tr>
                <th>Time (local)</th>
                <th>Caller ID</th>
                <th>Display Name</th>
                <th>Area</th>
                <th>Room</th>
                <th class="tts-col">TTS String</th>
                <th>Fusion Status</th>
                <th>Response Time</th>
                {% if view == 'advanced' %}
                <th>Attempts</th>
                <th>Last Error</th>
                <th>State</th>
                {% endif %}
            </tr>
        </thead>
        <tbody>
            {% if calls %}
                {% for call in calls %}
                <tr>
                    <td>{{ call.display_time }}</td>
                    <td>{{ call.caller_id }}</td>
                    <td>{{ call.display_name }}</td>
                    <td>{{ call.area_name }}{% if call.area_number %} ({{ call.area_number }}){% endif %}</td>
                    <td>{{ call.room_number if call.room_number is not none else '-' }}</td>
                    <td class="tts-col">{{ call.tts_string }}</td>
                    <td>
                        {% if call.fusion_status and call.fusion_status >= 200 and call.fusion_status < 300 %}
                            <span class="status-ok">{{ call.fusion_status }}</span>
                        {% elif call.fusion_status %}
                            <span class="status-err">{{ call.fusion_status }}</span>
                        {% else %}
                            <span class="status-pending">pending</span>
                        {% endif %}
                    </td>
                    <td>{{ "%.0f ms"|format(call.response_time_ms) if call.response_time_ms else '-' }}</td>
                    {% if view == 'advanced' %}
                    <td>{{ call.attempts if call.attempts is not none else '-' }}</td>
                    <td>{{ call.last_error if call.last_error else '-' }}</td>
                    <td>{{ call.state if call.state else '-' }}</td>
                    {% endif %}
                </tr>
                {% endfor %}
            {% else %}
                <tr><td colspan="{{ 11 if view == 'advanced' else 8 }}" class="empty-msg">No calls recorded yet.</td></tr>
            {% endif %}
        </tbody>
    </table>

    {% if total_pages > 1 %}
    <div class="pagination">
        {% if page > 1 %}
            <a href="?page={{ page - 1 }}&auto={{ '1' if auto_refresh else '0' }}&refresh={{ refresh_seconds }}&view={{ view }}">&laquo; Prev</a>
        {% else %}
            <span class="disabled">&laquo; Prev</span>
        {% endif %}

        {% for p in range(1, total_pages + 1) %}
            {% if p == page %}
                <span class="current">{{ p }}</span>
            {% elif p <= 3 or p > total_pages - 2 or (p >= page - 1 and p <= page + 1) %}
                <a href="?page={{ p }}&auto={{ '1' if auto_refresh else '0' }}&refresh={{ refresh_seconds }}&view={{ view }}">{{ p }}</a>
            {% elif p == 4 and page > 5 %}
                <span class="disabled">&hellip;</span>
            {% elif p == total_pages - 2 and page < total_pages - 4 %}
                <span class="disabled">&hellip;</span>
            {% endif %}
        {% endfor %}

        {% if page < total_pages %}
            <a href="?page={{ page + 1 }}&auto={{ '1' if auto_refresh else '0' }}&refresh={{ refresh_seconds }}&view={{ view }}">Next &raquo;</a>
        {% else %}
            <span class="disabled">Next &raquo;</span>
        {% endif %}
    </div>
    {% endif %}

    <h2 style="color: #00d4ff; margin-top: 30px; margin-bottom: 10px; font-size: 1.2rem;">Recent Logs</h2>
    <div class="log-panel">
        <button class="copy-btn" onclick="copyLog(this)">Copy</button>
        <pre style="background: #0d1117; border: 1px solid #0f3460; border-radius: 8px; padding: 15px; font-size: 0.78rem; line-height: 1.5; overflow-x: auto; max-height: 600px; overflow-y: auto; color: #c9d1d9; white-space: pre-wrap; word-wrap: break-word;">{% if log_lines %}{% for line in log_lines %}{{ line }}
{% endfor %}{% else %}No log file found.{% endif %}</pre>
    </div>

    {% if sip_debug_lines is not none %}
    <h2 style="color: #4fc3f7; margin-top: 30px; margin-bottom: 10px; font-size: 1.2rem;">SIP Messages Log</h2>
    <div class="log-panel">
        <button class="copy-btn" onclick="copyLog(this)">Copy</button>
        <pre style="background: #0d1117; border: 1px solid #0f3460; border-radius: 8px; padding: 15px; font-size: 0.78rem; line-height: 1.5; overflow-x: auto; max-height: 600px; overflow-y: auto; color: #4fc3f7; white-space: pre-wrap; word-wrap: break-word;">{% if sip_debug_lines %}{% for line in sip_debug_lines %}{{ line }}
{% endfor %}{% else %}No SIP debug entries yet.{% endif %}</pre>
    </div>
    {% endif %}

    {% if api_debug_lines is not none %}
    <h2 style="color: #ff9800; margin-top: 30px; margin-bottom: 10px; font-size: 1.2rem;">API Debug Log (Northbound)</h2>
    <div class="log-panel">
        <button class="copy-btn" onclick="copyLog(this)">Copy</button>
        <pre style="background: #0d1117; border: 1px solid #3d2800; border-radius: 8px; padding: 15px; font-size: 0.78rem; line-height: 1.5; overflow-x: auto; max-height: 600px; overflow-y: auto; color: #ffa657; white-space: pre-wrap; word-wrap: break-word;">{% if api_debug_lines %}{% for line in api_debug_lines %}{{ line }}
{% endfor %}{% else %}No API debug entries yet.{% endif %}</pre>
    </div>
    {% endif %}

    <script>
    function copyLog(btn) {
        var pre = btn.parentElement.querySelector('pre');
        var text = pre.textContent;
        // Try modern clipboard API first, fall back to execCommand for HTTP
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text).then(function() { showCopied(btn); });
        } else {
            var ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.left = '-9999px';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
            showCopied(btn);
        }
    }
    function showCopied(btn) {
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(function() {
            btn.textContent = 'Copy';
            btn.classList.remove('copied');
        }, 2000);
    }

    // Auto-refresh logic
    var autoCheck = document.getElementById('autoRefresh');
    var intervalSelect = document.getElementById('refreshInterval');
    var statusSpan = document.getElementById('refreshStatus');
    var timer = null;

    function buildUrl() {
        var params = new URLSearchParams(window.location.search);
        params.set('auto', autoCheck.checked ? '1' : '0');
        params.set('refresh', intervalSelect.value);
        // Stay on page 1 when auto-refreshing
        if (autoCheck.checked) params.set('page', '1');
        return '?' + params.toString();
    }

    function startRefresh() {
        stopRefresh();
        if (autoCheck.checked) {
            var secs = parseInt(intervalSelect.value);
            timer = setTimeout(function() { window.location.href = buildUrl(); }, secs * 1000);
            statusSpan.innerHTML = '&#9679; ON';
            statusSpan.className = 'refresh-indicator';
        } else {
            statusSpan.innerHTML = '&#9675; OFF';
            statusSpan.className = 'refresh-indicator off';
        }
    }

    function stopRefresh() {
        if (timer) { clearTimeout(timer); timer = null; }
    }

    autoCheck.addEventListener('change', startRefresh);
    intervalSelect.addEventListener('change', startRefresh);

    // Initialize
    startRefresh();

    function verifyLookups() {
        var btn = document.getElementById('verifyBtn');
        var status = document.getElementById('verifyStatus');
        var result = document.getElementById('verifyResult');
        btn.disabled = true;
        status.innerHTML = '<span style="color: #888;">Checking...</span>';
        result.style.display = 'none';

        fetch('/api/verify-lookups')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                btn.disabled = false;
                if (data.valid) {
                    status.innerHTML = '<span style="color: #4caf50;">&#10003; Valid — ' + data.summary + '</span>';
                    result.style.display = 'none';
                } else {
                    status.innerHTML = '<span style="color: #f44336;">&#10007; Problems found</span>';
                    var html = '<div style="background: #1c1017; border: 1px solid #5c1a1a; border-radius: 8px; padding: 15px; font-size: 0.82rem; color: #f8a0a0;">';
                    html += '<strong style="color: #f44336;">Validation Errors:</strong><br><br>';
                    for (var i = 0; i < data.errors.length; i++) {
                        html += '<div style="margin-bottom: 8px; padding-left: 12px; border-left: 2px solid #5c1a1a;">' + escapeHtml(data.errors[i]) + '</div>';
                    }
                    if (data.warnings && data.warnings.length > 0) {
                        html += '<br><strong style="color: #ff9800;">Warnings:</strong><br><br>';
                        for (var i = 0; i < data.warnings.length; i++) {
                            html += '<div style="margin-bottom: 8px; padding-left: 12px; border-left: 2px solid #5c3a00; color: #ffc080;">' + escapeHtml(data.warnings[i]) + '</div>';
                        }
                    }
                    html += '<br><a href="/api/sample-lookups" download="lookups-sample.yaml" style="color: #4fc3f7; text-decoration: underline;">Download sample lookups.yaml</a>';
                    html += '</div>';
                    result.innerHTML = html;
                    result.style.display = 'block';
                }
            })
            .catch(function(err) {
                btn.disabled = false;
                status.innerHTML = '<span style="color: #f44336;">Error: ' + err + '</span>';
            });
    }

    function escapeHtml(s) {
        var d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
    }
    </script>
</body>
</html>"""


SAMPLE_LOOKUPS_YAML = """\
# =============================================================================
# sipgw lookups.yaml — Lookup Tables for TTS Announcements
# =============================================================================
#
# This file defines how SIP caller information is translated into
# spoken text-to-speech (TTS) announcements. Edit this file and save —
# changes are picked up automatically without restarting the service.
#
# There are three types of mappings:
#
#   1. AREAS        — Maps area IDs to spoken area names
#   2. CALL PURPOSES — Maps display name keywords to spoken alert types
#   3. AREA+ROOM    — Maps area+room combos to spoken room names
#
# =============================================================================

# -----------------------------------------------------------------------------
# AREAS — Map area ID (from SIP username) to a spoken area name.
#
# The area ID comes from the SIP caller username format: a{area}r{room}b{bed}
# For example, in "a730r201b1", the area ID is "730".
#
# Use "..." (ellipsis) to create natural TTS pauses between phrases.
# The area name is spoken AFTER the alert type: "Code Blue! [area name] [room]"
# -----------------------------------------------------------------------------
areas:
  710: "3rd Floor... Cardiac Step-Down..."
  711: "2nd Floor... Orthopedics..."
  730: "1st Floor... E.D..."
  731: "4th Floor... I.C.U..."
  # Add more areas as needed. The key is the numeric area ID.

# Default spoken when the area ID is not found in the table above.
default_area: "Unknown Area."

# -----------------------------------------------------------------------------
# CALL PURPOSES — Map keywords found in the SIP display name to a spoken
# alert type. The display name is searched for each keyword (substring match).
# First match wins, so order matters.
#
# Example: If the SIP display name is "Code Blue Alert", the keyword "Blue"
# matches, and the spoken purpose becomes "Code Blue".
# -----------------------------------------------------------------------------
call_purposes:
  "Blue": "Code Blue"
  "RRT": "Rapid Response Team"
  "Pink": "Code Pink"
  # Add more as needed. Key = keyword to search for, Value = spoken text.

# Default spoken when no keyword matches (or display name is empty).
default_purpose: "Code"

# -----------------------------------------------------------------------------
# ROOMS (fallback) — Map room numbers to spoken room names.
# These apply globally regardless of area. Use area_rooms below for
# area-specific overrides. Leave empty if all room names are area-specific.
# -----------------------------------------------------------------------------
rooms: {}

# -----------------------------------------------------------------------------
# AREA+ROOM COMBO OVERRIDES — Map area+room combinations to spoken room names.
#
# This is the PRIMARY room naming mechanism. Use this when the same room
# number exists in different areas with different meanings.
#
# Format: "area*room": "spoken name"
#   - area = the area ID from the SIP username
#   - room = the room number from the SIP username (leading zeros preserved)
#   - The spoken name will have a period appended automatically.
#
# Lookup priority:
#   1. area_rooms match  (e.g., "797*2201" -> "Prepost 1.")
#   2. rooms match       (fallback, if rooms section has entries)
#   3. default format    (e.g., "Room 2201.")
#
# EXAMPLES:
#   Room 2201 in area 797 (Heart Center) = "Prepost 1"
#   Room 2201 in area 795 (Ortho East)   = no override -> "Room 2201."
#   Room 01196 in area 730 (E.D.)        = "B 15" (leading zeros preserved)
# -----------------------------------------------------------------------------
area_rooms:
  "797*2201": "Prepost 1"
  "797*2202": "Prepost 2"
  "730*01196": "B 15"
  "710*3196": "Dialysis"
  # Add more as needed.

# Default format when room is not found in any lookup.
# {room} is replaced with the actual room number (leading zeros preserved).
default_room_format: "Room {room}."
"""


def _validate_lookups(lookups_path: str) -> dict:
    """Validate the lookups.yaml file and return detailed results."""
    errors = []
    warnings = []

    # Check file exists
    if not os.path.exists(lookups_path):
        return {"valid": False, "errors": [f"File not found: {lookups_path}"], "warnings": [], "summary": ""}

    # Check readable
    try:
        with open(lookups_path, "r") as f:
            raw_text = f.read()
    except Exception as e:
        return {"valid": False, "errors": [f"Cannot read file: {e}"], "warnings": [], "summary": ""}

    # Check YAML parseable
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as e:
        return {"valid": False, "errors": [f"YAML parse error: {e}"], "warnings": [], "summary": ""}

    if not isinstance(data, dict):
        return {"valid": False, "errors": ["File must contain a YAML mapping (key: value pairs), got: " + type(data).__name__], "warnings": [], "summary": ""}

    # Check required sections
    for section in ["areas", "call_purposes", "area_rooms"]:
        if section not in data:
            errors.append(f"Missing required section: '{section}'")
        elif not isinstance(data[section], dict):
            errors.append(f"Section '{section}' must be a mapping (key: value), got: {type(data[section]).__name__}")

    # Validate areas
    areas = data.get("areas", {})
    if isinstance(areas, dict):
        for k, v in areas.items():
            if not isinstance(v, str):
                errors.append(f"areas[{k}]: value must be a string, got {type(v).__name__}: {v!r}")
            elif not v.strip():
                warnings.append(f"areas[{k}]: value is empty")

    # Validate call_purposes
    purposes = data.get("call_purposes", {})
    if isinstance(purposes, dict):
        for k, v in purposes.items():
            if not isinstance(k, str):
                errors.append(f"call_purposes: key must be a string, got {type(k).__name__}: {k!r}")
            if not isinstance(v, str):
                errors.append(f"call_purposes[{k}]: value must be a string, got {type(v).__name__}: {v!r}")

    # Validate rooms (if present)
    rooms = data.get("rooms", {})
    if rooms and isinstance(rooms, dict):
        for k, v in rooms.items():
            if not isinstance(v, str):
                errors.append(f"rooms[{k}]: value must be a string, got {type(v).__name__}: {v!r}")

    # Validate area_rooms
    area_rooms = data.get("area_rooms", {})
    if isinstance(area_rooms, dict):
        area_ids = {str(k) for k in areas} if isinstance(areas, dict) else set()
        for k, v in area_rooms.items():
            k_str = str(k)
            if "*" not in k_str:
                errors.append(f"area_rooms[{k}]: key must be in 'area*room' format (missing '*')")
            else:
                parts = k_str.split("*")
                if len(parts) != 2:
                    errors.append(f"area_rooms[{k}]: key must have exactly one '*' separator, got {len(parts)-1}")
                elif not parts[0] or not parts[1]:
                    errors.append(f"area_rooms[{k}]: area and room parts cannot be empty")
                elif area_ids and parts[0] not in area_ids:
                    warnings.append(f"area_rooms[{k}]: area '{parts[0]}' not found in areas section")
            if not isinstance(v, str):
                errors.append(f"area_rooms[{k}]: value must be a string, got {type(v).__name__}: {v!r}")
            elif not v.strip():
                warnings.append(f"area_rooms[{k}]: value is empty")

    # Validate default_room_format
    fmt = data.get("default_room_format", "")
    if fmt and "{room}" not in fmt:
        warnings.append(f"default_room_format: missing {{room}} placeholder: {fmt!r}")

    # Summary
    area_count = len(areas) if isinstance(areas, dict) else 0
    purpose_count = len(purposes) if isinstance(purposes, dict) else 0
    room_count = len(rooms) if isinstance(rooms, dict) else 0
    ar_count = len(area_rooms) if isinstance(area_rooms, dict) else 0

    summary = f"{area_count} areas, {purpose_count} purposes, {room_count} rooms, {ar_count} area+room overrides"

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "summary": summary,
    }


def _read_log_tail(log_path: str, num_lines: int = 50) -> list[str]:
    """Read the last N lines from the log file."""
    try:
        p = Path(log_path)
        if not p.exists():
            return []
        with open(p, "rb") as f:
            # Seek from end to efficiently read tail
            f.seek(0, 2)
            size = f.tell()
            # Read up to 512KB from the end (API debug responses can be large)
            chunk_size = min(size, 524288)
            f.seek(size - chunk_size)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        return lines[-num_lines:]
    except Exception:
        logger.exception("Failed to read log file")
        return []


def create_dashboard(db: CallDatabase, config: DashboardConfig,
                     log_config: Optional[LoggingConfig] = None,
                     health_config=None) -> FastAPI:
    """Create the FastAPI dashboard application."""
    from jinja2 import Environment

    app = FastAPI(title="sipgw Dashboard", docs_url=None, redoc_url=None)
    env = Environment(autoescape=True)
    template = env.from_string(DASHBOARD_HTML)

    log_dir = Path(log_config.log_dir) if log_config else Path("/var/log/sipgw")
    log_file = str(log_dir / "sipgw.log")
    api_debug_file = str(log_dir / "sipgw_api_debug.log")
    sip_debug_file = str(log_dir / "sipgw_sip_debug.log")
    api_debug_enabled = log_config.api_debug_log if log_config else False
    sip_debug_enabled = log_config.sip_debug_log if log_config else False

    @app.get("/", response_class=HTMLResponse)
    async def index(
        page: int = Query(1, ge=1),
        auto: int = Query(0, ge=0, le=1),
        refresh: int = Query(config.auto_refresh_seconds),
        view: str = Query("summary"),
    ):
        # #13-P1: invalid view value falls back to summary (never a 500).
        if view not in ("summary", "advanced"):
            view = "summary"
        page_size = config.page_size
        calls, total_calls, total_pages = await db.get_calls_page(
            page=page, page_size=page_size, today_only=True,
        )

        # #12: stored timestamps are UTC; nurses see local wall time derived
        # from the canonical created_at epoch (not the raw stored string).
        tz_name = log_config.timezone if log_config else ""   # "" = host local
        for c in calls:
            c["display_time"] = display_local(c.get("created_at"), tz_name)

        stats = await db.get_today_stats()
        success = stats["success"]
        failed = stats["failed"]
        pending = stats.get("pending", 0)

        log_lines = _read_log_tail(log_file)
        api_debug_lines = _read_log_tail(api_debug_file) if api_debug_enabled else None
        sip_debug_lines = _read_log_tail(sip_debug_file) if sip_debug_enabled else None

        # Clamp refresh to allowed values
        if refresh not in (10, 30, 60, 120, 300):
            refresh = config.auto_refresh_seconds

        html = template.render(
            calls=calls,
            total_calls=total_calls,
            success_calls=success,
            failed_calls=failed,
            pending_calls=pending,
            page=page,
            total_pages=total_pages,
            auto_refresh=bool(auto),
            refresh_seconds=refresh,
            view=view,
            log_lines=log_lines,
            api_debug_lines=api_debug_lines,
            sip_debug_lines=sip_debug_lines,
        )
        return HTMLResponse(content=html)

    @app.get("/api/calls")
    async def api_calls(limit: int = 100):
        """JSON API for recent calls."""
        calls = await db.get_recent_calls(limit=limit)
        return {"calls": calls}

    @app.get("/export.csv")
    async def export_csv():
        """#13-P1: stream today's REAL calls as CSV.

        db.export_calls enforces 'AND is_test=0', so no dry-run/test row can
        leak. Rows are quoted by the stdlib csv module (not HTML-escaped).
        """
        rows = await db.export_calls(today_only=True)
        tz_name = log_config.timezone if log_config else ""   # "" = host local

        def _safe(v):
            # Guard against CSV/formula injection when opened in a spreadsheet:
            # neutralize a leading = + - @ (and control chars) on text cells.
            s = "" if v is None else str(v)
            if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
                s = "'" + s
            return s

        def generate():
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([
                "Time (local)", "Caller ID", "Area", "Room",
                "TTS String", "State", "Fusion Status",
            ])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            for r in rows:
                area = r.get("area_name") or ""
                if r.get("area_number"):
                    area = f"{area} ({r.get('area_number')})".strip()
                room = r.get("room_number")
                writer.writerow([
                    display_local(r.get("created_at"), tz_name),
                    _safe(r.get("caller_id")),
                    _safe(area),
                    _safe(room if room is not None else ""),
                    _safe(r.get("tts_string")),
                    _safe(r.get("state")),
                    r.get("fusion_status") if r.get("fusion_status") is not None else "",
                ])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

        today = display_local(time.time(), tz_name)[:10]   # YYYY-MM-DD (local)
        filename = f"sipgw-calls-{today}.csv"
        return StreamingResponse(
            generate(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    lookups_file = os.environ.get("SIPGW_LOOKUPS", "/opt/sipgw/lookups.yaml")

    @app.get("/api/verify-lookups")
    async def verify_lookups():
        """Validate the lookups.yaml file and return detailed results."""
        result = _validate_lookups(lookups_file)
        return JSONResponse(content=result)

    @app.get("/api/sample-lookups")
    async def sample_lookups():
        """Download a sample lookups.yaml with detailed commentary."""
        return PlainTextResponse(
            content=SAMPLE_LOOKUPS_YAML,
            media_type="application/x-yaml",
            headers={"Content-Disposition": "attachment; filename=lookups-sample.yaml"},
        )

    stale_after = getattr(health_config, "stale_after_seconds", 30.0)

    @app.get("/health")
    async def health():
        # #7 real liveness: healthy only if the writer's heartbeat is fresh.
        beat = await db.read_heartbeat("writer")
        if beat is None:
            return JSONResponse(status_code=503, content={"status": "no-heartbeat"})
        age = time.time() - beat
        if age > stale_after:
            return JSONResponse(status_code=503,
                                content={"status": "stale", "heartbeat_age_s": round(age, 1)})
        return {"status": "ok", "heartbeat_age_s": round(age, 1)}

    return app
