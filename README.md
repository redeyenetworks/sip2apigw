# sip2apigw — SIP-to-Webhook Gateway for Nurse Call Systems

A production Python asyncio service that bridges **Rauland nurse call systems** with **Informacast Fusion** mass notification. When a Code Blue, Rapid Response Team, or Code Pink alert is initiated, the system receives the inbound SIP call, parses caller information from SIP headers, builds a text-to-speech announcement, and triggers a Fusion scenario that broadcasts the alert to IP speakers.

**v1.6.0** is the reliability + observability release. A page is now **recorded before it is sent** (record-first), delivered by a **durable retry worker** that survives a Fusion outage or a process crash, **escalated to a human channel** if it can never be delivered, and observed through a real heartbeat-backed `/health`. The dashboard runs as a **separate read-only process**, and the whole system is designed around a set of hard life-safety invariants (see [Safety Model](#safety-model)).

---

## How It Works

```
Rauland Nurse Call                sipgw (writer)                 Informacast Fusion
─────────────────       ──────────────────────────────       ─────────────────────
                        ┌──────────────────────────┐
   SIP INVITE ────────> │ 1. Receive INVITE        │
                        │ 2. 100 Trying            │
              <──────── │ 3. 200 OK                │
                        │ 4. Send BYE (immediate)  │
                        │ 5. Parse caller          │
                        │ 6. Build TTS             │
                        │ 7. RECORD-FIRST:         │
                        │    persist PENDING row   │ ── record-first, then dedupe (shadow)
                        └───────────┬──────────────┘
                                    │ (durable outbox)
                        ┌───────────▼──────────────┐
                        │ Delivery worker (#2)     │ ──────> OAuth2 Token (kept warm, #4)
                        │  retry + backoff         │ ──────> POST /scenario-notifications
                        │  escalate on fail (#3)   │ <────── 200 OK + TTS Audio
                        │  expire if too old       │
                        └──────────────────────────┘         Speakers announce:
                                                             "Attention! Code Blue!
   sipgw-dashboard (read-only process, #14)                   1st Floor. E.D.
   ┌───────────────────────────────┐                          Room 201."
   │ Call history + state-aware     │
   │ stats, CSV export, log viewers │
   │ /health  ◄── writer heartbeat  │
   └───────────────────────────────┘  :8080 (read-only DB)
```

### Two-service topology (#14)

v1.6.0 splits the single process into two systemd units that share one `config.yaml` and one SQLite database:

| Service | Unit | Role |
|---------|------|------|
| **Writer** | `sipgw.service` | SIP listener + record-first + durable delivery worker + escalation + heartbeat + systemd watchdog. **Owns all writes** to the DB and the shared log files. |
| **Dashboard** | `sipgw-dashboard.service` | Read-only web UI + `/health`. Opens the DB `query_only=ON` so it **can never mutate a page or the heartbeat**, and writes only its own `sipgw_dashboard.log`. |

Both processes bootstrap identically: `load_config` → effective-dry-run → **prod-DB barrier** → `validate_config`. A misconfigured or unsafe dashboard refuses to start exactly like the writer does. **Both services must run for a healthy system.**

### Example

SIP call from `"Code Blue" <sip:a730r201@172.16.1.100>` with default config produces:

```
Attention! Attention! Code Blue! 1st Floor. E.D. Room 201. Code Blue! 1st Floor.
E.D. Room 201. Code Blue! 1st Floor. E.D. Room 201.
```

This is delivered as TTS audio to all IP speakers in the configured Fusion device group.

---

## What's New in v1.6.0

### Reliability

- **#2 Durable, record-first delivery** — On call answer, the page is written to the `calls` outbox as a `pending` row **before** any send is attempted (`create_pending_call`). A background `DeliveryWorker` then delivers it asynchronously with **exponential backoff** (honoring `Retry-After` delta-seconds), so a Fusion outage or a crash between "answered" and "sent" cannot drop a Code Blue. The state machine is `pending → delivering → delivered | failed | expired` (plus `legacy` for pre-v1.6.0 rows). On startup, `recover_inflight()` returns any crash-orphaned `delivering` rows to `pending` — **at-least-once** delivery. The DB runs in **WAL** mode so the read-only dashboard can read while the writer commits.
- **#3 Escalation on failure/expiry** — When a page exhausts `delivery.max_attempts` (`failed`) or ages past `delivery.max_age_seconds` (`expired`), the `Escalator` POSTs a JSON alert to a human channel (`escalation.webhook_url` — Teams/Slack/PagerDuty/NOC). Escalation failures are logged, never raised, and never disrupt delivery. Empty URL still logs the failure loudly at ERROR.
- **#4 Background OAuth2 token refresh** — A background task keeps the Fusion token warm, renewing it `token_refresh_margin_seconds` (default 300s) before expiry, so a real page never blocks on a token round-trip on the critical path.
- **#8 systemd `Type=notify` watchdog** — The writer sends `READY=1` **before** running crash-recovery (a large recover must not delay READY and trip the restart loop), then pings `WATCHDOG=1` on a cadence (`WATCHDOG_USEC/2`). Watchdog pings prove **event-loop liveness only** — decoupled from DB writes, so transient DB slowness never restarts the life-safety pager. Completely **inert without systemd** (`NOTIFY_SOCKET`/`WATCHDOG_USEC` unset): tests, dry-run, and non-systemd runs behave exactly as before.
- **#9 Startup config validation** — `validate_config` runs before the service starts and is **fatal on invalid production config**: Fusion credentials, `scenario_id`, and a **preset** `scenario_field_id` are required in prod so the first real Code Blue does not fail auth or trigger a live field-id lookup. CIDR ranges, ports, and retry tuning are validated too. Non-fatal issues surface as warnings.

### Observability

- **#7 Heartbeat-backed `/health`** — The writer stamps a `heartbeat` row every `health.heartbeat_interval_seconds`; the dashboard reads it and returns **`200 {"status":"ok"}`** only if the beat is fresh, **`503`** if it is stale (`health.stale_after_seconds`, default 30s) or absent. `/health` now reflects real writer liveness, not just "the web server answered".
- **#10 State-aware, test-excluding stats** — Dashboard cards derive success/failed/pending from the delivery **state** (`get_today_stats`), exclude all `is_test=1` rows, and classify `legacy` rows by their stored `fusion_status` for continuity across the cutover boundary.
- **#12 Canonical UTC timestamps, host-local display** — Every row's `timestamp` is written as canonical **UTC RFC3339 millis-`Z`**. Bucketing and the "today" boundary key off the numeric `created_at` epoch (uniform across old local-format and new UTC rows), while the dashboard and CSV render **host-local** wall-clock. `logging.timezone: ""` reads the host's local zone; an IANA name overrides per install.
- **#13-P1 Dashboard view toggle + CSV export** — A **Summary / Advanced** view toggle, plus `GET /export.csv` which exports today's **real** calls (invalid `view` falls back to summary, never a 500). CSV always appends `AND is_test=0`, so dry-run/test rows can never leak into an exported file.
- **#6 Async logging** — File writes, daily rotation, and `.tgz` compression happen on a background `QueueListener` thread (via `QueueHandler`), so the event loop never blocks on disk I/O. The dashboard uses a **dashboard-safe** logging setup that never attaches the rotating handler to the writer's shared log files (two processes racing midnight `doRollover()` would corrupt them).
- **#11 Logging hygiene** — `client_id` + `client_secret` and bearer tokens masked in all debug logs, exception **types** logged, BYE `Via` transport corrected.
- **#15 INVITE fingerprint** — `invite_fingerprint(msg)` computes a stable **transaction identity** (`v1:<hex>` from Call-ID + From user + From tag + CSeq) so a UDP retransmission of the same INVITE is recognizable. This is deliberately **distinct** from #5's clinical identity (see below).

### Dedupe (#5) — ships SHADOW / DISABLED

Clinical dedupe computes a stable **clinical identity** for a page — normalized `(area, room, bed, purpose)` as `cf-v1:<hex>` — to measure how often true duplicates arrive. **It ships inert and never drops a page:**

- **Two OFF switches**, both default off:
  - `dedupe.enforce: false` — never suppresses. Setting it `true` is a **fatal** config error (`validate_config` forbids it in all modes; suppression requires clinical sign-off).
  - `dedupe.window_seconds: 0` — the shadow duplicate lookup **never even runs**; `evaluate` returns a fingerprint-only, no-suppress decision without touching the DB. A test-only `window_seconds > 0` turns on `WOULD suppress …` telemetry, and even then the page is still delivered.
- **Record-first is sacred.** Dedupe runs **after** `create_pending_call`, purely as telemetry (it may annotate `duplicate_of` and log). It **never gates, delays, or skips** delivery. **A real second Code Blue for the same room is always sent.**
- The clinical fingerprint (`cf-v1:`) is intentionally **not** the same as #15's transaction fingerprint (`v1:`). The two are separate, clearly named functions and must never be conflated.

---

## Safety Model

These invariants are load-bearing and enforced in code and tests:

- **No real send in dev/test** — `NoSendGuardTransport` (`sipgw/safety.py`) backs every outbound HTTP client (Fusion webhook **and** escalation) when dry-run is active. Effective dry-run = `fusion.dry_run` **OR** env `SIPGW_DRY_RUN=1`; the environment can only **enable** dry-run, never disable it.
- **`[TEST]` marking** — In dry-run, the `[TEST]` marker is installed on the loggers **first**, so every log line (including init lines) is marked, and every DB row is written `is_test=1`.
- **Prod-DB hard barrier** — `assert_safe_database_path` runs on **every** DB open — including the read-only dashboard reader — so dry-run/test can never attach to the production database.
- **Never weakened** — SIP IP allowlist, credential masking, and Jinja `autoescape=True`.

---

## Features

- **SIP UA** — Listens on UDP/TCP port 5060. Handles INVITE, ACK, BYE, CANCEL, OPTIONS.
- **Immediate BYE mode** — Answer and hang up instantly without RTP (mirrors existing systems).
- **Caller parsing** — Extracts area, room, bed from SIP username format `a{area}r{room}[b{bed}]`. Leading zeros preserved (e.g., `r01196` stays `01196`).
- **Configurable TTS** — Play count (default 3x), message preamble, iteration preamble.
- **Area+Room combo overrides** — Same room number, different name per area.
- **Fusion integration** — OAuth2 client credentials with token caching + background refresh, field-based scenario trigger, auto field-id resolution.
- **Hot-reload lookups** — `lookups.yaml` changes detected on next SIP call or page load, no restart needed.
- **Durable record-first delivery** — retry/backoff, crash recovery, expiry, escalation.
- **Read-only web dashboard** — dark-themed, auto-refreshing history, state-aware stats, view toggle, CSV export, log viewers, heartbeat-backed `/health`.
- **3 debug logs** — application, SIP messages, API — async-written, daily rotation with `.tgz` compression, 90-day retention.
- **266 tests** — unit, functional, and system-level.

---

## Quick Start

```bash
# Install (as root) — installs BOTH services
sudo bash install.sh

# Configure (one file, shared by both services)
sudo vi /opt/sipgw/config.yaml     # Set fusion credentials + preset scenario_field_id
sudo vi /opt/sipgw/lookups.yaml    # Review area/room mappings (hot-reloaded)

# Start both services
sudo systemctl start sipgw            # writer (SIP + delivery)
sudo systemctl start sipgw-dashboard  # read-only dashboard + /health

# Verify
systemctl status sipgw sipgw-dashboard
curl http://localhost:8080/health     # 200 only if the writer heartbeat is fresh
```

---

## Configuration

All settings are in a single `config.yaml`, loaded by both services. Full reference in [docs/CONFIGURATION.md](docs/CONFIGURATION.md) and [docs/SIPGW_SERVICE_MANUAL.md](docs/SIPGW_SERVICE_MANUAL.md).

### SIP

| Parameter | Default | Description |
|-----------|---------|-------------|
| `sip.bind_ip` | `0.0.0.0` | IP to bind SIP listener |
| `sip.bind_port` | `5060` | SIP port (UDP + TCP) |
| `sip.allowed_networks` | `["172.16.0.0/12"]` | CIDR ranges allowed to send SIP |
| `sip.call_timeout_seconds` | `600` | Max call duration before auto-BYE |
| `sip.immediate_bye` | `false` | Answer then immediately BYE (no RTP) |
| `sip.rtp_port_range_start` / `_end` | `10000` / `20000` | RTP port range |

### Fusion API

| Parameter | Default | Description |
|-----------|---------|-------------|
| `fusion.base_url` | `https://api.icmobile.singlewire.com/api` | Fusion API base URL |
| `fusion.token_url` | `.../api/token` | OAuth2 token endpoint |
| `fusion.audience` | | Provider ID (required in prod) |
| `fusion.scenario_id` | | Scenario UUID (required in prod) |
| `fusion.variable_name` | `customTTS` | Scenario field variable name |
| `fusion.scenario_field_id` | | Field UUID (**must be preset in prod**; auto-resolved only in dry-run) |
| `fusion.client_id` / `client_secret` | | OAuth2 credentials (required in prod) |
| `fusion.token_refresh_margin_seconds` | `300` | Refresh the token this long before expiry (#4) |
| `fusion.dry_run` | `false` | Force no-send guard on all outbound HTTP (env `SIPGW_DRY_RUN=1` can also enable) |

### Delivery (#2 retry worker)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `delivery.max_attempts` | `6` | Attempts before a page is marked `failed` + escalated |
| `delivery.base_backoff_seconds` | `2.0` | Base for exponential backoff |
| `delivery.max_backoff_seconds` | `60.0` | Backoff cap (also caps honored `Retry-After`) |
| `delivery.max_age_seconds` | `900.0` | Undelivered longer than this → `expired` + escalate |
| `delivery.poll_interval_seconds` | `1.0` | Worker poll cadence |
| `delivery.batch_size` | `20` | Rows processed per pass |

### Escalation (#3) / Health (#7) / Dedupe (#5)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `escalation.webhook_url` | `""` | Human alert channel; empty logs failures at ERROR only |
| `escalation.timeout_seconds` | `10.0` | Escalation POST timeout |
| `health.heartbeat_interval_seconds` | `10.0` | Writer heartbeat cadence |
| `health.stale_after_seconds` | `30.0` | `/health` returns 503 once the heartbeat is older than this |
| `dedupe.enforce` | `false` | **Must stay false** — `true` is a fatal config error |
| `dedupe.window_seconds` | `0` | `0` = shadow lookup never runs; `>0` = test-only `WOULD suppress` telemetry |

### Logging / Dashboard / Database

| Parameter | Default | Description |
|-----------|---------|-------------|
| `logging.log_dir` | `/var/log/sipgw` | Log directory |
| `logging.retention_days` | `90` | Days to keep rotated `.tgz` logs |
| `logging.timezone` | `""` | Display/day-boundary zone; `""` = host local, or an IANA name (#12) |
| `logging.api_debug_log` / `sip_debug_log` | `true` | Enable detailed API / SIP logs |
| `dashboard.port` / `bind_ip` | `8080` / `0.0.0.0` | Dashboard listener |
| `dashboard.auto_refresh_seconds` | `30` | Default page auto-refresh interval |
| `dashboard.page_size` | `20` | Rows per page |
| `database.path` | `/var/lib/sipgw/calls.db` | Shared SQLite DB (writer RW, dashboard read-only) |

---

## Lookup Tables

`lookups.yaml` maps SIP caller data to speech-ready names. **Changes are hot-reloaded automatically** — the file's mtime is checked on every lookup (SIP call or dashboard page load); no restart needed. Use the "Verify lookups.yaml" dashboard button to validate after editing.

```yaml
areas:
  730: "1st Floor... E.D..."
  731: "4th Floor... I.C.U..."
default_area: "Unknown Area."

call_purposes:            # display-name keyword → call purpose (first match wins)
  "Blue": "Code Blue"
  "RRT": "Rapid Response Team"
  "Pink": "Code Pink"
default_purpose: "Code Blue"

rooms: {}                 # room-only fallback

area_rooms:               # "area*room" → spoken name (handles duplicate room numbers)
  "797*2201": "Prepost 1"
  "730*01196": "B 15"     # leading zeros preserved

default_room_format: "Room {room}."
```

**Lookup priority:** `area_rooms` combo match → `rooms` fallback → `"Room {N}."`

---

## Fusion API Integration

1. **Token**: `POST {base_url}/token` with `client_credentials` grant + `audience` (provider ID), kept warm by the background refresher (#4).
2. **Trigger**: `POST {base_url}/v1/scenario-notifications?scenarioId={id}`
3. **Body**: `{"fields": [{"fieldId": "<uuid>", "answer": "<assembled TTS>"}]}`

In production `scenario_field_id` must be preset; auto-resolution runs only in dry-run.

### Required Fusion Setup

1. Create + enable an OAuth2 application; note the client ID/secret.
2. Add scope `urn:singlewire:scenario-notifications:write`.
3. Create a scenario with a text field variable (e.g., `customTTS`) and a message template using `{{customTTS}}`.

---

## Dashboard

Read-only web UI at `http://<host>:8080` — no authentication required. Served by the separate `sipgw-dashboard` process against a **read-only** DB connection.

- **Call table** — Summary / Advanced view toggle; timestamps shown in host-local time.
- **State-aware stats cards** — total, successful, failed, pending; test rows excluded; legacy rows classified by stored status.
- **CSV export** — `GET /export.csv` (today's real calls; test rows never leak).
- **Log viewers** — `sipgw.log`, SIP messages, API debug, each with a Copy button.
- **Lookup verification** — `GET /api/verify-lookups`, `GET /api/sample-lookups`, plus the Verify button.
- **JSON API** — `GET /api/calls?limit=100`.
- **Health check** — `GET /health` returns `200 {"status":"ok","heartbeat_age_s":…}` only when the writer heartbeat is fresh; `503` (`stale` / `no-heartbeat`) otherwise.

---

## Logging

| Log File | Content | Written by |
|----------|---------|-----------|
| `/var/log/sipgw/sipgw.log` | Application events, call processing, errors | writer |
| `/var/log/sipgw/sipgw_sip_debug.log` | Raw SIP messages (both directions) | writer |
| `/var/log/sipgw/sipgw_api_debug.log` | Full HTTP request/response traces to Fusion | writer |
| `/var/log/sipgw/sipgw_dashboard.log` | Dashboard process log (separate file — never the writer's) | dashboard |

All writer logs are written off the event loop (#6), rotate daily at midnight, compress to `.tgz`, and are retained 90 days. In dry-run every line is `[TEST]`-marked.

---

## SIP Implementation

Lightweight custom SIP UA (not pjsua2/sipsimple), purpose-built for narrow requirements:

| Method | Handling |
|--------|----------|
| INVITE | 100 Trying → 200 OK (with SDP) → optional RTP → optional immediate BYE |
| ACK | Acknowledged silently |
| BYE | 200 OK → terminate call + cleanup |
| CANCEL | 200 OK + 487 Request Terminated |
| OPTIONS | 200 OK with capabilities |

RTP silence: 12-byte header + 160 bytes of `0xFF` (u-law silence), 20ms intervals, PCMU/8000. `invite_fingerprint(msg)` (#15) provides a stable transaction identity for correlating retransmissions.

---

## Testing

**266 tests** pass across the suite:

```bash
cd /opt/sipgw && SIPGW_LOOKUPS=/opt/sipgw/lookups.yaml \
  ./venv/bin/python -m pytest -q
```

Coverage includes the new v1.6.0 subsystems: `test_delivery`, `test_escalation`, `test_token_refresh`, `test_dedupe`, `test_health`, `test_watchdog`, `test_migration`, `test_readonly_db`, `test_stats`, `test_timestamps`, `test_startup_safety`, `test_no_send`, `test_safety_barrier_marker`, `test_logging_hygiene`, `test_async_logging`, `test_invite_fingerprint`, `test_dashboard_app`, `test_config_validation`, alongside the original parser/lookups/TTS/SIP/RTP/webhook/dashboard/functional/system tests.

---

## Service Management

```bash
# Writer (SIP + durable delivery)
sudo systemctl {start|stop|restart|status} sipgw

# Read-only dashboard + /health
sudo systemctl {start|stop|restart|status} sipgw-dashboard

journalctl -u sipgw -f
journalctl -u sipgw-dashboard -f
```

---

## Security

- **IP filtering** — `sip.allowed_networks` CIDR ranges (empty = reject all).
- **No-send guard** — `NoSendGuardTransport` blocks all outbound HTTP in dry-run.
- **Prod-DB barrier** — enforced on every DB open, writer and reader alike.
- **systemd hardening** — `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`; writer uses `CAP_NET_BIND_SERVICE` for port 5060; dashboard has its own `MemoryMax=256M` / `CPUQuota=50%` envelope so a runaway UI request can't starve the life-safety writer.
- **Read-only dashboard** — `query_only=ON`; cannot mutate pages or the heartbeat.
- **Token lock** — `asyncio.Lock` + double-check prevents concurrent token refresh races.
- **XSS protection** — Jinja2 `autoescape=True` on all dashboard output.
- **Secret masking** — `client_id`/`client_secret` and bearer tokens truncated in debug logs.

---

## Project Structure

```
/opt/sipgw/
├── config.yaml                  # Single config, shared by both services
├── lookups.yaml                 # Area/purpose/room lookup tables
├── sipgw.service                # systemd unit — writer (Type=notify watchdog)
├── sipgw-dashboard.service      # systemd unit — read-only dashboard
├── install.sh / uninstall.sh
├── sipgw/
│   ├── main.py                  # Writer entry point + SIPGateway orchestrator
│   ├── dashboard_app.py         # Dashboard entry point (read-only, #14)
│   ├── sip_server.py            # SIP UA (UDP+TCP)
│   ├── sip_message.py           # SIP parser + response builder + invite_fingerprint (#15)
│   ├── rtp_handler.py           # RTP silence stream (u-law PCMU/8000)
│   ├── parser.py                # Caller info parser
│   ├── lookups.py               # Lookup table loader (hot-reload)
│   ├── tts_builder.py           # TTS builder + assembler
│   ├── webhook.py               # Fusion client (OAuth2, background refresh #4)
│   ├── delivery.py              # Durable delivery worker (#2)
│   ├── escalation.py            # Human-channel escalation (#3)
│   ├── dedupe.py                # Clinical dedupe — SHADOW/DISABLED (#5)
│   ├── watchdog.py              # systemd Type=notify + watchdog (#8)
│   ├── database.py              # SQLite state machine, heartbeat, stats, timestamps
│   ├── dashboard.py             # FastAPI UI (view toggle, CSV, /health)
│   ├── config.py                # Typed config loader + validate_config (#9)
│   ├── logging_config.py        # Async logging + dashboard-safe setup (#6)
│   └── safety.py                # No-send guard, [TEST] marker, prod-DB barrier
├── tests/                       # 266 tests
└── docs/
    ├── ARCHITECTURE.md   ASSUMPTIONS.md   CONFIGURATION.md
    ├── TESTING.md        SIPGW_SERVICE_MANUAL.md
    └── RUNBOOK-cutover-2026-07-01.md

/var/log/sipgw/  sipgw.log · sipgw_sip_debug.log · sipgw_api_debug.log · sipgw_dashboard.log
/var/lib/sipgw/  calls.db (+ -wal / -shm sidecars)
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SIPGW_CONFIG` | `/opt/sipgw/config.yaml` | Path to configuration file |
| `SIPGW_LOOKUPS` | `/opt/sipgw/lookups.yaml` | Path to lookup tables |
| `SIPGW_DRY_RUN` | unset | `1` forces effective dry-run (can only **enable**, never disable) |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Writer won't start | `journalctl -u sipgw` — `validate_config` is fatal on missing prod credentials / `scenario_field_id`, bad CIDR, port conflict |
| `/health` returns 503 | Writer heartbeat stale/absent — check `sipgw.service` is running; the dashboard alone cannot be healthy |
| Pages stuck `pending` | Check Fusion reachability / credentials; the worker retries with backoff and escalates on exhaustion |
| Pages `expired` | Undelivered past `delivery.max_age_seconds` — inspect escalation channel + Fusion outage window |
| 401/403 from Fusion | Verify credentials + scope `urn:singlewire:scenario-notifications:write` |
| Service refuses to start in dry-run | Prod-DB barrier — dry-run/test cannot open the production `database.path` |

---

## Deferred / Host-Gated Items

These require real hardware, a live capture, or clinical sign-off and are **not** exercised by the test suite:

- **Real-systemd watchdog + OOM isolation drills** — `Type=notify` READY/`WATCHDOG=1` behavior, and the dashboard `MemoryMax`/`CPUQuota` isolation, only take effect under real systemd with the relevant cgroup controllers. Validate on the target host, not in CI/containers.
- **#5 dedupe enforcement** — remains SHADOW/DISABLED. Turning suppression on requires **clinical sign-off** and validation against a **real Rauland INVITE capture**; `enforce=true` is a fatal config error until then.
- **Production cutover** — see [docs/RUNBOOK-cutover-2026-07-01.md](docs/RUNBOOK-cutover-2026-07-01.md) for the no-data-loss lookups/config preservation gate and cutover steps.

---

## Documentation

- Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Configuration reference: [docs/CONFIGURATION.md](docs/CONFIGURATION.md)
- Assumptions: [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md)
- Testing: [docs/TESTING.md](docs/TESTING.md)
- Service manual: [docs/SIPGW_SERVICE_MANUAL.md](docs/SIPGW_SERVICE_MANUAL.md)
- Cutover runbook: [docs/RUNBOOK-cutover-2026-07-01.md](docs/RUNBOOK-cutover-2026-07-01.md)
- Changelog: [CHANGELOG.md](CHANGELOG.md)

---

## Prerequisites

- Ubuntu 22.04+ / Debian 12+
- Python 3.11+
- Network access to Informacast Fusion API (`api.icmobile.singlewire.com`)
- SIP traffic from the nurse call system on port 5060

---

## Uninstall

```bash
sudo bash uninstall.sh
```
