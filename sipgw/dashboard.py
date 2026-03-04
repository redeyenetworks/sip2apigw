"""FastAPI dashboard for sipgw.

Provides a web UI showing call history with pagination, auto-refresh toggle,
log viewers, and health endpoint. No authentication required.
"""

import logging
from pathlib import Path
from fastapi import FastAPI, Request, Query
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

    {% if total_pages > 1 %}
    <div class="pagination">
        {% if page > 1 %}
            <a href="?page={{ page - 1 }}&auto={{ '1' if auto_refresh else '0' }}&refresh={{ refresh_seconds }}">&laquo; Prev</a>
        {% else %}
            <span class="disabled">&laquo; Prev</span>
        {% endif %}

        {% for p in range(1, total_pages + 1) %}
            {% if p == page %}
                <span class="current">{{ p }}</span>
            {% elif p <= 3 or p > total_pages - 2 or (p >= page - 1 and p <= page + 1) %}
                <a href="?page={{ p }}&auto={{ '1' if auto_refresh else '0' }}&refresh={{ refresh_seconds }}">{{ p }}</a>
            {% elif p == 4 and page > 5 %}
                <span class="disabled">&hellip;</span>
            {% elif p == total_pages - 2 and page < total_pages - 4 %}
                <span class="disabled">&hellip;</span>
            {% endif %}
        {% endfor %}

        {% if page < total_pages %}
            <a href="?page={{ page + 1 }}&auto={{ '1' if auto_refresh else '0' }}&refresh={{ refresh_seconds }}">Next &raquo;</a>
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
        navigator.clipboard.writeText(pre.textContent).then(function() {
            btn.textContent = 'Copied!';
            btn.classList.add('copied');
            setTimeout(function() {
                btn.textContent = 'Copy';
                btn.classList.remove('copied');
            }, 2000);
        });
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
    sip_debug_file = str(log_dir / "sipgw_sip_debug.log")
    api_debug_enabled = log_config.api_debug_log if log_config else False
    sip_debug_enabled = log_config.sip_debug_log if log_config else False

    @app.get("/", response_class=HTMLResponse)
    async def index(
        page: int = Query(1, ge=1),
        auto: int = Query(0, ge=0, le=1),
        refresh: int = Query(config.auto_refresh_seconds),
    ):
        page_size = config.page_size
        calls, total_calls, total_pages = await db.get_calls_page(
            page=page, page_size=page_size, today_only=True,
        )

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
        sip_debug_lines = _read_log_tail(sip_debug_file) if sip_debug_enabled else None

        # Clamp refresh to allowed values
        if refresh not in (10, 30, 60, 120, 300):
            refresh = config.auto_refresh_seconds

        html = template.render(
            calls=calls,
            total_calls=total_calls,
            success_calls=success,
            failed_calls=failed,
            page=page,
            total_pages=total_pages,
            auto_refresh=bool(auto),
            refresh_seconds=refresh,
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

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app
