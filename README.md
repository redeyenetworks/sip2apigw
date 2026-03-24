# sip2apigw — SIP-to-Webhook Gateway for Nurse Call Systems

A production Python asyncio service that bridges **Rauland nurse call systems** with **Informacast Fusion** mass notification. When a Code Blue, Rapid Response Team, or Code Pink alert is initiated, the system receives the inbound SIP call, parses caller information from SIP headers, builds a text-to-speech announcement, and triggers a Fusion scenario that broadcasts the alert to IP speakers.

---

## How It Works

```
Rauland Nurse Call                sipgw                     Informacast Fusion
─────────────────       ─────────────────────────       ─────────────────────
                        ┌─────────────────────┐
   SIP INVITE ────────> │ 1. Receive INVITE   │
                        │ 2. 100 Trying       │
              <──────── │ 3. 200 OK           │
                        │ 4. Send BYE         │ ──────> OAuth2 Token Request
              <──────── │    (immediate mode)  │ <────── Token Response
                        │ 5. Parse caller     │
                        │ 6. Build TTS        │ ──────> POST /scenario-notifications
                        │ 7. Trigger webhook  │ <────── 200 OK + TTS Audio
                        │ 8. Record to DB     │
                        └─────────────────────┘         Speakers announce:
                                                        "Attention! Code Blue!
                         Dashboard :8080                  1st Floor. E.D.
                         ┌───────────────┐                Room 201."
                         │ Call history  │
                         │ SIP log       │
                         │ API log       │
                         └───────────────┘
```

### Example

SIP call from `"Code Blue" <sip:a730r201@172.16.1.100>` with default config produces:

```
Attention! Attention! Code Blue! 1st Floor. E.D. Room 201. Code Blue! 1st Floor.
E.D. Room 201. Code Blue! 1st Floor. E.D. Room 201.
```

This is delivered as TTS audio (12.8 seconds) to all IP speakers in the configured Fusion device group.

---

## Features

- **SIP UA** — Listens on UDP/TCP port 5060. Handles INVITE, ACK, BYE, CANCEL, OPTIONS.
- **Immediate BYE mode** — Answer and hang up instantly without RTP (mirrors existing systems).
- **Caller parsing** — Extracts area, room, bed from SIP username format `a{area}r{room}[b{bed}]`. Leading zeros preserved (e.g., `r01196` stays `01196`).
- **Configurable TTS** — Play count (default 3x), message preamble, iteration preamble.
- **Area+Room combo overrides** — Same room number, different name per area (e.g., room 2201 = "Prepost 1" in Heart Center, "Room 2201" in Ortho East).
- **Fusion integration** — OAuth2 client credentials with token caching, field-based scenario trigger.
- **Auto field resolution** — Discovers the Fusion scenario field UUID automatically on first call.
- **Hot-reload lookups** — `lookups.yaml` changes detected on next SIP call or page load, no restart needed.
- **Verify lookups button** — Dashboard button validates YAML with detailed error reporting + sample download.
- **Web dashboard** — Dark-themed, auto-refreshing call history with stats at `:8080`.
- **3 debug logs** — Application log, SIP messages log, API debug log — all viewable on dashboard with Copy buttons.
- **Log rotation** — Daily rotation at midnight with .tgz compression, 90-day retention.
- **SQLite history** — All calls recorded with timestamps, parsed data, TTS, Fusion status, response times.
- **Concurrent calls** — Handles multiple simultaneous SIP calls and webhook triggers.
- **Security** — IP filtering, systemd hardening, credential masking in logs, XSS-safe dashboard.
- **120 tests** — Unit, functional, and system-level tests across 9 test files.
- **Automated backups** — Daily backup to `/home/sipgw/backups/` with 30-day retention.

---

## Quick Start

```bash
# Install (as root)
sudo bash install.sh

# Configure
sudo vi /opt/sipgw/config.yaml     # Set fusion credentials
sudo vi /opt/sipgw/lookups.yaml    # Review area/room mappings (hot-reloaded)

# Start
sudo systemctl start sipgw

# Verify
systemctl status sipgw
curl http://localhost:8080/health
```

---

## Configuration

All settings are in `config.yaml`. Full reference in [docs/SIPGW_SERVICE_MANUAL.md](docs/SIPGW_SERVICE_MANUAL.md).

### SIP

| Parameter | Default | Description |
|-----------|---------|-------------|
| `sip.bind_ip` | `0.0.0.0` | IP to bind SIP listener |
| `sip.bind_port` | `5060` | SIP port (UDP + TCP) |
| `sip.allowed_networks` | `["172.16.0.0/12"]` | CIDR ranges allowed to send SIP |
| `sip.call_timeout_seconds` | `600` | Max call duration before auto-BYE |
| `sip.immediate_bye` | `false` | Answer then immediately BYE (no RTP) |
| `sip.rtp_port_range_start` | `10000` | RTP port range start |
| `sip.rtp_port_range_end` | `20000` | RTP port range end |

### Fusion API

| Parameter | Default | Description |
|-----------|---------|-------------|
| `fusion.base_url` | `https://api.icmobile.singlewire.com/api` | Fusion API base URL |
| `fusion.token_url` | `https://api.icmobile.singlewire.com/api/token` | OAuth2 token endpoint |
| `fusion.audience` | | Provider ID (from admin console URL) |
| `fusion.scenario_id` | | Scenario UUID to trigger |
| `fusion.variable_name` | `customTTS` | Scenario field variable name |
| `fusion.scenario_field_id` | | Field UUID (auto-resolved if empty) |
| `fusion.client_id` | | OAuth2 client ID |
| `fusion.client_secret` | | OAuth2 client secret |

### TTS Assembly

| Parameter | Default | Description |
|-----------|---------|-------------|
| `tts.play_count` | `3` | Number of times TTS content repeats |
| `tts.message_preamble` | `"Attention! "` | Prepended once at start of entire message |
| `tts.iteration_preamble` | `"Attention! "` | Prepended before each repetition |

**Assembly formula:** `{message_preamble}{iteration_preamble}{base} {iteration_preamble}{base} ...`

### Logging

| Parameter | Default | Description |
|-----------|---------|-------------|
| `logging.log_dir` | `/var/log/sipgw` | Log directory |
| `logging.retention_days` | `90` | Days to keep rotated logs |
| `logging.api_debug_log` | `true` | Enable detailed northbound API logging |
| `logging.sip_debug_log` | `true` | Enable detailed SIP message logging |

### Dashboard

| Parameter | Default | Description |
|-----------|---------|-------------|
| `dashboard.port` | `8080` | Web dashboard port |
| `dashboard.auto_refresh_seconds` | `10` | Page auto-refresh interval |

---

## Lookup Tables

`lookups.yaml` maps SIP caller data to speech-ready names. **Changes are hot-reloaded automatically** — no restart needed. The file's mtime is checked on every lookup (SIP call or dashboard page load). Reload is confirmed by a log entry in `sipgw.log`. Use the "Verify lookups.yaml" button on the dashboard to validate after editing (clicking it also triggers the reload).

```yaml
# Area ID → spoken name (use "..." for TTS pauses)
areas:
  730: "1st Floor... E.D..."
  731: "4th Floor... I.C.U..."
  797: "2nd Floor... Heart Center..."
default_area: "Unknown Area."

# Display name keywords → call purpose (first match wins)
call_purposes:
  "Blue": "Code Blue"
  "RRT": "Rapid Response Team"
  "Pink": "Code Pink"
default_purpose: "Code Blue"

# Room-only fallback (empty — all overrides use area_rooms below)
rooms: {}

# Area+Room combo overrides — handles duplicate room numbers across areas
# Format: "area*room": "spoken name"
area_rooms:
  "797*2201": "Prepost 1"     # room 2201 in Heart Center = Prepost 1
  "730*01196": "B 15"          # room 01196 in E.D. = B 15 (leading zeros preserved)
  "710*3196": "Dialysis"       # room 3196 in Cardiac Step-Down = Dialysis
  # Room 2201 in area 795 (Ortho East) has no override → "Room 2201."

default_room_format: "Room {room}."
```

**Lookup priority:** `area_rooms` combo match → `rooms` fallback → `"Room {N}."`

---

## TTS Composition

### Step 1: Base TTS (`build_tts`)

Parses caller info and constructs: `{purpose}! {area_name} {room_text}`

Example: `"Code Blue! 1st Floor. E.D. Room 201."`

### Step 2: Assembly (`assemble_tts`)

Applies preambles and repetition:

```
Given:  play_count=3, message_preamble="Attention! ", iteration_preamble=""
Result: "Attention! Code Blue! 1st Floor. E.D. Room 201. Code Blue! 1st Floor.
         E.D. Room 201. Code Blue! 1st Floor. E.D. Room 201."
```

---

## Fusion API Integration

The service uses the Singlewire Informacast Fusion REST API:

1. **Token**: `POST {base_url}/token` with `client_credentials` grant + `audience` (provider ID)
2. **Trigger**: `POST {base_url}/v1/scenario-notifications?scenarioId={id}`
3. **Body**: `{"fields": [{"fieldId": "<uuid>", "answer": "<assembled TTS>"}]}`

The `fieldId` is auto-resolved from the scenario definition on first call if `scenario_field_id` is not set in config.

### Required Fusion Setup

1. Create an OAuth2 application in the Fusion admin console
2. Enable the application and note the client ID/secret
3. Add scope: `urn:singlewire:scenario-notifications:write`
4. Create a scenario with a text field variable (e.g., `customTTS`)
5. Configure a message template that uses `{{customTTS}}` for TTS content

---

## Dashboard

Web UI at `http://<host>:8080` — no authentication required.

- **Call table** — timestamp, caller ID, display name, area, room, TTS string, Fusion status, response time
- **Stats cards** — total, successful, and failed calls
- **Recent Logs** — last 50 lines of `sipgw.log` with Copy button
- **SIP Messages Log** — full inbound/outbound SIP messages with Copy button
- **API Debug Log** — complete HTTP request/response traces with Copy button
- **JSON API** — `GET /api/calls?limit=100`
- **Verify lookups** — button validates `lookups.yaml` with detailed error/warning output + sample download
- **Verify API** — `GET /api/verify-lookups` (JSON validation results)
- **Sample lookups** — `GET /api/sample-lookups` (downloadable annotated YAML)
- **Health check** — `GET /health`

---

## Logging

| Log File | Content | Toggle |
|----------|---------|--------|
| `/var/log/sipgw/sipgw.log` | Application events, call processing, errors | Always on |
| `/var/log/sipgw/sipgw_sip_debug.log` | Raw SIP messages (full headers + bodies, both directions) | `logging.sip_debug_log` |
| `/var/log/sipgw/sipgw_api_debug.log` | Full HTTP request/response traces to Fusion API | `logging.api_debug_log` |

All logs rotate daily at midnight, compressed to `.tgz`, retained for 90 days.

```bash
# View logs
tail -f /var/log/sipgw/sipgw.log
tail -f /var/log/sipgw/sipgw_sip_debug.log
tail -f /var/log/sipgw/sipgw_api_debug.log
journalctl -u sipgw -f
```

---

## Immediate BYE Mode

When `sip.immediate_bye: true`, the service answers and immediately hangs up:

```
Caller               sipgw
  |--- INVITE -------->|
  |<-- 100 Trying -----|
  |<-- 200 OK ---------|
  |<-- BYE ------------|   (no RTP stream started)
  |--- 200 OK (BYE) -->|
```

This mirrors the behavior of the existing nurse call receiver being replaced. The webhook fires asynchronously after the BYE.

---

## SIP Implementation

Lightweight custom SIP UA (not pjsua2/sipsimple) — purpose-built for narrow requirements:

| Method | Handling |
|--------|----------|
| INVITE | 100 Trying → 200 OK (with SDP) → optional RTP → optional immediate BYE |
| ACK | Acknowledged silently |
| BYE | 200 OK → terminate call + cleanup |
| CANCEL | 200 OK + 487 Request Terminated |
| OPTIONS | 200 OK with capabilities |

RTP silence: 12-byte header + 160 bytes of `0xFF` (u-law silence), 20ms intervals, PCMU/8000.

---

## Testing

120 tests across 9 files:

```bash
/opt/sipgw/venv/bin/python -m pytest tests/ -v
```

| Test File | Count | Coverage |
|-----------|-------|----------|
| `test_parser.py` | 13 | SIP username parsing, From header extraction, leading zeros |
| `test_lookups.py` | 20 | Area, purpose, room, area+room combo lookups, leading zeros |
| `test_tts_builder.py` | 19 | TTS building, area+room combos, assembly, leading zeros |
| `test_sip_message.py` | 10 | SIP message parsing and response building |
| `test_rtp.py` | 10 | RTP packet construction |
| `test_webhook.py` | 4 | OAuth2 token handling and scenario triggering |
| `test_dashboard.py` | 7 | FastAPI dashboard endpoints |
| `test_functional.py` | 14 | End-to-end pipeline, database, config, assembly |
| `test_system.py` | 3 | Real UDP socket tests (INVITE, IP filter, OPTIONS) |

---

## Service Management

```bash
sudo systemctl start sipgw
sudo systemctl stop sipgw
sudo systemctl restart sipgw
systemctl status sipgw
```

---

## Backups

Automated daily backups at 2:00 AM to `/home/sipgw/backups/`:

```bash
# Manual backup
sudo bash /home/sipgw/backups/sipgw-backup.sh

# List backups
ls -lh /home/sipgw/backups/sipgw-backup-*.tar.gz
```

Includes all code, config, database, logs, and systemd unit. 30-day retention. See [backups/RESTORE.md](/home/sipgw/backups/RESTORE.md) for restore procedures.

---

## Security

- **IP filtering** — `sip.allowed_networks` CIDR ranges
- **systemd hardening** — `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`
- **Dedicated user** — runs as `sipgw` with no login shell
- **File permissions** — `config.yaml` is 640 (contains credentials)
- **Port binding** — `CAP_NET_BIND_SERVICE` instead of running as root
- **Token lock** — asyncio.Lock prevents concurrent token refresh races
- **XSS protection** — Jinja2 autoescape on all dashboard output
- **Secret masking** — credentials truncated in all debug logs

---

## Project Structure

```
/opt/sipgw/
├── config.yaml              # Main configuration
├── lookups.yaml             # Area/purpose/room lookup tables
├── requirements.txt         # Python dependencies
├── sipgw.service            # systemd unit file
├── install.sh               # Installation script
├── uninstall.sh             # Uninstallation script
├── sipgw/                   # Python package
│   ├── main.py              # Entry point + SIPGateway orchestrator
│   ├── sip_server.py        # SIP UA (UDP+TCP, INVITE/ACK/BYE/CANCEL/OPTIONS)
│   ├── sip_message.py       # SIP message parser + response builder
│   ├── rtp_handler.py       # RTP silence stream (u-law PCMU/8000)
│   ├── parser.py            # Caller info parser (area/room/bed extraction)
│   ├── lookups.py           # Lookup table loader (areas, purposes, rooms)
│   ├── tts_builder.py       # TTS builder + assembler (preambles, repetition)
│   ├── webhook.py           # Fusion API client (OAuth2, scenario trigger)
│   ├── database.py          # SQLite call history (aiosqlite)
│   ├── dashboard.py         # FastAPI web dashboard (Jinja2, autoescape)
│   ├── config.py            # Typed dataclass config loader
│   └── logging_config.py    # Logging setup (rotation, compression, debug logs)
├── tests/                   # 120 tests across 9 files
└── docs/
    └── SIPGW_SERVICE_MANUAL.md  # Comprehensive service manual
```

```
/var/log/sipgw/              # Log files (daily rotation, .tgz compression)
├── sipgw.log                # Application log
├── sipgw_sip_debug.log      # SIP message traces
└── sipgw_api_debug.log      # Fusion API request/response traces

/var/lib/sipgw/
└── calls.db                 # SQLite call history
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SIPGW_CONFIG` | `/opt/sipgw/config.yaml` | Path to configuration file |
| `SIPGW_LOOKUPS` | `/opt/sipgw/lookups.yaml` | Path to lookup tables |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Service won't start | `journalctl -u sipgw` — check config.yaml syntax, port 5060 conflict |
| 401 from Fusion | Verify client_id/client_secret, ensure app is enabled in admin console |
| 403 from Fusion | Add scope `urn:singlewire:scenario-notifications:write` to the app |
| TTS not playing | Check `scenario_field_id` — verify in API debug log that `answer` field is populated |
| SIP calls rejected | Add caller's IP range to `sip.allowed_networks` |
| Stale OAuth2 token | `sudo systemctl restart sipgw` to clear token cache |

---

## Documentation

- Full service manual: [docs/SIPGW_SERVICE_MANUAL.md](docs/SIPGW_SERVICE_MANUAL.md)
- Changelog: [CHANGELOG.md](CHANGELOG.md)

---

## Prerequisites

- Ubuntu 22.04+ / Debian 12+
- Python 3.11+
- Network access to Informacast Fusion API (`api.icmobile.singlewire.com`)
- SIP traffic from nurse call system on port 5060

---

## Uninstall

```bash
sudo bash uninstall.sh
```
