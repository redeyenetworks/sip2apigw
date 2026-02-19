"""FastAPI dashboard for sipgw.

Provides a web UI showing recent call history with auto-refresh.
No authentication required.
"""

import logging
from collections import deque
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from typing import Optional

from .database import CallDatabase
from .config import DashboardConfig, LoggingConfig

logger = logging.getLogger("sipgw.dashboard")

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>sipgw Dashboard</title>
    <meta http-equiv="refresh" content="{{ refresh_seconds }}">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #e0e0e0;
            padding: 20px;
        }
        h1 {
            color: #00d4ff;
            margin-bottom: 5px;
            font-size: 1.5rem;
        }
        .subtitle {
            color: #888;
            margin-bottom: 20px;
            font-size: 0.85rem;
        }
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
        .log-panel {
            position: relative;
            margin-top: 10px;
        }
        .log-panel pre {
            margin: 0;
        }
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
    <h1>sipgw Dashboard</h1>
    <p class="subtitle">Auto-refresh every {{ refresh_seconds }}s &bull; {{ total_calls }} total calls</p>

    <div class="stats">
        <div class="stat-card">
            <div class="label">Total Calls</div>
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
    </div>

    <table>
        <thead>
            <tr>
                <th>Timestamp</th>
                <th>Caller ID</th>
                <th>Display Name</th>
                <th>Area</th>
                <th>Room</th>
                <th class="tts-col">TTS String</th>
                <th>Fusion Status</th>
                <th>Response Time</th>
            </tr>
        </thead>
        <tbody>
            {% if calls %}
                {% for call in calls %}
                <tr>
                    <td>{{ call.timestamp }}</td>
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
                </tr>
                {% endfor %}
            {% else %}
                <tr><td colspan="8" class="empty-msg">No calls recorded yet.</td></tr>
            {% endif %}
        </tbody>
    </table>

    <h2 style="color: #00d4ff; margin-top: 30px; margin-bottom: 10px; font-size: 1.2rem;">Recent Logs</h2>
    <div class="log-panel">
        <button class="copy-btn" onclick="copyLog(this)">Copy</button>
        <pre style="background: #0d1117; border: 1px solid #0f3460; border-radius: 8px; padding: 15px; font-size: 0.78rem; line-height: 1.5; overflow-x: auto; max-height: 600px; overflow-y: auto; color: #c9d1d9; white-space: pre-wrap; word-wrap: break-word;">{% if log_lines %}{% for line in log_lines %}{{ line }}
{% endfor %}{% else %}No log file found.{% endif %}</pre>
    </div>

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
        navigator.clipboard.writeText(pre.textContent).then(function() {
            btn.textContent = 'Copied!';
            btn.classList.add('copied');
            setTimeout(function() {
                btn.textContent = 'Copy';
                btn.classList.remove('copied');
            }, 2000);
        });
    }
    </script>
</body>
</html>"""


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


def create_dashboard(db: CallDatabase, config: DashboardConfig, log_config: Optional[LoggingConfig] = None) -> FastAPI:
    """Create the FastAPI dashboard application."""
    from jinja2 import Environment

    app = FastAPI(title="sipgw Dashboard", docs_url=None, redoc_url=None)
    env = Environment(autoescape=True)
    template = env.from_string(DASHBOARD_HTML)

    log_dir = Path(log_config.log_dir) if log_config else Path("/var/log/sipgw")
    log_file = str(log_dir / "sipgw.log")
    api_debug_file = str(log_dir / "sipgw_api_debug.log")
    api_debug_enabled = log_config.api_debug_log if log_config else False

    @app.get("/", response_class=HTMLResponse)
    async def index():
        calls = await db.get_recent_calls(limit=200)
        total = len(calls)
        success = sum(
            1 for c in calls
            if c.get("fusion_status") and 200 <= c["fusion_status"] < 300
        )
        failed = sum(
            1 for c in calls
            if c.get("fusion_status") and (c["fusion_status"] < 200 or c["fusion_status"] >= 300)
        )

        log_lines = _read_log_tail(log_file)
        api_debug_lines = _read_log_tail(api_debug_file) if api_debug_enabled else None

        html = template.render(
            calls=calls,
            total_calls=total,
            success_calls=success,
            failed_calls=failed,
            refresh_seconds=config.auto_refresh_seconds,
            log_lines=log_lines,
            api_debug_lines=api_debug_lines,
        )
        return HTMLResponse(content=html)

    @app.get("/api/calls")
    async def api_calls(limit: int = 100):
        """JSON API for recent calls."""
        calls = await db.get_recent_calls(limit=limit)
        return {"calls": calls}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app
