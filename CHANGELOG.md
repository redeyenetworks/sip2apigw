# Changelog

All notable changes to the sipgw project are documented in this file.

---

## [v1.6.0] — 2026-07-01

Reliability + observability release. The gateway moves from best-effort,
single-process paging to a **durable, record-first delivery pipeline** with retry,
escalation, and a decoupled read-only dashboard. Every change here preserves the
life-safety invariants: no real outbound send in dev/test (`NoSendGuardTransport`),
`[TEST]`-marked logs and `is_test=1` rows under dry-run, the prod-DB path barrier on
every DB open, and — above all — **a real page is never dropped, duplicated, or gated
by any new machinery.**

### Added
- **Durable, record-first delivery (#2)** — `on_call` now writes the page to the DB
  in state `pending` **before** any delivery is attempted (`main.py` record-first
  path). A background `DeliveryWorker` (`delivery.py`) drives the state machine
  `pending → delivering → delivered | failed | expired` (`database.py`, WAL mode,
  idempotent schema migration; legacy rows carry state `legacy`). If the process
  restarts, `recover()` re-queues in-flight pages so nothing is lost.
- **Retry with exponential backoff (#2)** — failed attempts retry with
  `base_backoff_seconds` (default 2.0s) doubling up to `max_backoff_seconds`
  (default 60s), honoring an upstream `Retry-After` delta-seconds when present.
  After `delivery.max_attempts` (default 6) a page is marked `failed`; a page
  undelivered past `delivery.max_age_seconds` (default 900s) is marked `expired`.
  Poll cadence is `poll_interval_seconds` (default 1.0s).
- **Escalation on failed/expired pages (#3)** — `escalation.py` posts to a human
  alert channel (`escalation.webhook_url` — Teams/Slack/PagerDuty/NOC) when a page
  ends in `failed` or `expired`. Empty URL disables escalation (failures still
  logged at ERROR). In dry-run the escalation HTTP client is routed through the
  no-send guard like every other client.
- **Background OAuth2 token refresh (#4)** — the token is refreshed proactively
  `fusion.token_refresh_margin_seconds` (default 300s) before expiry (`webhook.py`),
  so a page never blocks on a token round-trip on the hot path.
- **Clinical dedupe in SHADOW / DISABLED (#5)** — `dedupe.py` computes a stable
  **clinical** fingerprint `cf-v1:<hex>` over the normalized
  (area, room, bed, purpose) tuple — deliberately distinct from #15's SIP
  transaction fingerprint. It ships **inert** behind two OFF switches:
  `dedupe.enforce=false` (never suppresses) and `dedupe.window_seconds=0` (the
  shadow duplicate lookup never even queries the DB). Setting `window_seconds>0` is
  test-only telemetry that logs `WOULD suppress …` but **still returns no-suppress**;
  `enforce=true` is out-of-policy and made **fatal** by `validate_config`. The
  deduper runs *after* `create_pending_call` and never gates the insert — **a real
  second Code Blue for the same room is always delivered.**
- **Async logging (#6)** — a non-blocking `QueueHandler` moves all file writes,
  rotation, and gzip compression off the event loop (`logging_config.py`;
  `CompressingTimedRotatingFileHandler` on `sipgw.log`, `sipgw_api_debug.log`,
  `sipgw_sip_debug.log`).
- **Real `/health` backed by a writer heartbeat (#7)** — the writer stamps a
  `heartbeat` row every `dashboard.heartbeat_interval_seconds` (default 10s;
  `database.write_heartbeat`); the dashboard's `/health` reads it
  (`read_heartbeat`) and reports unhealthy if older than
  `dashboard.stale_after_seconds` (default 30s) — a single source of truth, no
  duplicate dashboard key.
- **systemd `Type=notify` watchdog (#8)** — `watchdog.py`; `main` sends `READY=1`
  once listeners are up (before `recover()`) and pings `WATCHDOG` on a cadence, so
  a hung event loop is detected and the writer restarted. Fully inert without real
  systemd (containers/CI). `StartLimitIntervalSec=0` keeps start-rate limiting from
  ever wedging the life-safety pager in `failed`.
- **Startup config validation (#9)** — `validate_config` fails fatally on invalid
  production config (e.g. `max_attempts < 1`, `poll_interval_seconds <= 0`,
  `dedupe.enforce=true`) and warns on soft issues (e.g. empty
  `escalation.webhook_url`).
- **State-aware, test-excluding dashboard stats (#10)** — stats derive from the
  delivery state machine and exclude `is_test=1` rows; a **Pending** card was added
  alongside Successful/Failed.
- **Canonical UTC RFC3339-Z timestamps (#12)** — the writer stamps atomic
  `…Z` UTC timestamps (`database._utc_rfc3339`); day-boundary bucketing keys off
  the numeric `created_at` epoch (`_day_start_epoch`), and the dashboard renders
  **host-local** time ("Time (local)", `display_local`). `logging.timezone=""`
  means host-local (hosts are UTC); an IANA name overrides.
- **Two-service split: writer + read-only dashboard (#14)** — the dashboard is now
  a separate process (`dashboard_app.py`, `sipgw-dashboard.service`) that opens the
  DB **read-only** (`query_only`) and only reads the writer's heartbeat. The sd_notify
  watchdog stays exclusively on the life-safety writer (`sipgw.service`). The
  dashboard runs under its own `MemoryMax=256M` / `CPUQuota=50%` envelope so a
  runaway UI request cannot starve the writer (enforced only under real systemd
  cgroups; inert in CI/containers).
- **Dashboard view toggle + CSV export (#13-P1)** — Summary/Advanced view toggle
  (invalid `view` values fall back to `summary`, never a 500) and an `/export.csv`
  endpoint.
- **Stable INVITE fingerprint (#15)** — `invite_fingerprint(msg)` (`sip_message.py`)
  yields a `v1:<hex>` SIP **transaction** identity (Call-ID/From/CSeq) for
  correlation and as the basis for #5. Kept clearly separate from #5's `cf-v1:`
  clinical identity.

### Changed
- `on_call` is now record-first: the DB insert precedes delivery and is never
  gated by dedupe or any downstream check.
- Dashboard is decoupled from the writer and can be restarted independently
  without affecting paging.

### Safety / invariants
- Dry-run can only be **enabled**, never disabled; any new HTTP client
  (escalation) uses the `NoSendGuardTransport`. All test logs are `[TEST]`-marked
  and all test rows are `is_test=1`. The prod-DB path barrier
  (`assert_safe_database_path`) runs on **every** DB open, including the dashboard's
  read-only connection. SIP IP allowlist, credential masking, and Jinja
  `autoescape=True` are unchanged.

### Deferred / gated on humans + real hosts
- **Real-systemd watchdog + OOM isolation drills** — the `Type=notify` watchdog and
  the dashboard `MemoryMax`/`CPUQuota` envelope are inert in CI/containers and must
  be exercised under real systemd with cgroup controllers before cutover.
- **#5 dedupe enforcement** stays SHADOW/DISABLED until clinical sign-off **and** a
  real Rauland INVITE capture validate the clinical fingerprint against production
  traffic; suppression is `validate_config`-forbidden today.
- **Production cutover** of the two-service split remains a manual, operator-run step.

---

## [v1.5.1] — 2026-03-24

### Fixed
- **Dashboard stats capped at page size** — "Successful" and "Failed" counts were computed from the current page (max 20 rows) instead of all today's calls. Now uses a dedicated SQL query (`get_today_stats()`) that counts across all of today's records.
- **Copy buttons not working over HTTP** — `navigator.clipboard.writeText()` requires HTTPS or localhost. Added `document.execCommand('copy')` fallback for plain HTTP connections over LAN.

---

## [v1.5] — 2026-03-24

### Added
- **Area+Room combo overrides** (`area_rooms:` in lookups.yaml) — maps `"area*room"` keys to spoken room names, handling duplicate room numbers across areas. 211 authoritative overrides. Lookup priority: area_rooms combo → rooms fallback → default format.
- **Hot-reload of lookups.yaml** — file changes detected automatically via mtime check on every lookup call. No service restart needed. Failed reloads logged with full traceback; previous data preserved.
- **Verify lookups.yaml button** on dashboard — validates YAML syntax, required sections, key formats, value types, and cross-references. Shows detailed per-entry error messages and warnings.
- **Sample lookups download** (`/api/sample-lookups`) — downloadable `lookups-sample.yaml` with extensive commentary documenting all mapping types.
- `/api/verify-lookups` JSON endpoint for programmatic validation.
- 120 tests across 9 test files.

### Changed
- Room naming now uses `get_room_name(room, area)` — area parameter enables combo lookups.
- Removed all 305 room-only entries from `lookups.yaml` — all overrides now use `area_rooms` combos.
- `tts_builder.py` passes `area_number` to `get_room_name()` for combo lookup support.

---

## [v1.4] — 2026-03-24

### Fixed
- **Leading zeros stripped from room/area numbers** — `a730*r01196*b1` was parsed as room `1196` instead of `01196`. Area, room, and bed numbers are now stored as strings throughout the entire pipeline (parser, lookups, database, TTS builder, dashboard) to preserve the original format from SIP headers.

### Changed
- `CallerInfo.area_number`, `room_number`, `bed_number` types changed from `Optional[int]` to `Optional[str]`.
- Lookup table keys (`areas`, `rooms`) changed from `int` to `str` internally.
- Database schema: `area_number` and `room_number` columns changed from `INTEGER` to `TEXT`.
- All 112 tests updated to use string values for area/room/bed assertions.

### Added
- Leading-zero preservation tests in parser, TTS builder, and lookups test suites.
- Updated `lookups.yaml` with expanded room mappings (~500 entries) and ellipsis-style TTS pauses in area names.

---

## [v1.3] — 2026-03-04

### Added
- **Dashboard pagination** — calls displayed 20 per page (configurable `dashboard.page_size`), today's calls only.
- **Auto-refresh toggle** — disabled by default, with dropdown for 10s/30s/60s/120s/300s intervals.
- **Default refresh interval** configurable via `dashboard.auto_refresh_seconds` (default 30s).
- Client-side JS manages refresh timer; state preserved in URL query params.

### Changed
- Stats cards now show today's totals instead of all-time.
- Removed server-side `<meta http-equiv="refresh">` tag in favor of client-controlled timer.

---

## [v1.2] — 2026-03-04

### Added
- **Immediate BYE mode** (`sip.immediate_bye`) — answer and immediately hang up without RTP stream, mirroring the existing nurse call receiver's SIP behavior.
- **SIP messages debug log** (`sipgw_sip_debug.log`) — full raw inbound/outbound SIP message capture with `<<< RECV` / `>>> SEND` markers.
- **SIP Messages Log panel** on dashboard (light blue theme) with Copy button.
- `logging.sip_debug_log` config toggle.

### Fixed
- **Token refresh race condition** — added `asyncio.Lock` with double-check pattern to prevent concurrent coroutines from duplicating token requests.
- **RTP port leak** — ports are now freed if INVITE setup fails after allocation.
- **XSS vulnerability** — dashboard switched from `Jinja2.Template()` to `Environment(autoescape=True)`.
- **Token JSON validation** — explicit `JSONDecodeError` handling and `access_token` key check with clear error messages.

---

## [v1.1] — 2026-02-19

### Added
- **TTS assembly** — configurable `play_count` (default 3), `message_preamble`, and `iteration_preamble` in new `tts:` config section.
- **Room number mapping** — `rooms:` section in `lookups.yaml` for overriding "Room N" with custom names.
- `get_room_name()` lookup function with fallback to `default_room_format`.
- **Fusion fieldId-based payload** — scenario trigger now sends `{"fields": [{"fieldId": "...", "answer": "..."}]}` for proper TTS delivery.
- **Auto field resolution** — `scenario_field_id` discovered automatically from Fusion API on first call.
- **API debug log** (`sipgw_api_debug.log`) — full HTTP request/response traces for northbound API.
- **Dashboard log panels** — Recent Logs, API Debug Log with Copy buttons.
- **Comprehensive service manual** (`docs/SIPGW_SERVICE_MANUAL.md`).
- **Automated backups** — daily at 2:00 AM with 30-day retention.
- 105+ tests across 9 test files.

---

## [v1.0] — 2026-02-19

### Initial Release
- SIP UA on UDP/TCP port 5060 with IP filtering.
- SIP INVITE/ACK/BYE/CANCEL/OPTIONS handling.
- RTP silence stream (u-law PCMU/8000, 20ms intervals).
- Caller info parsing from SIP From header (`a{area}r{room}[b{bed}]`).
- TTS string builder with area/purpose lookups from `lookups.yaml`.
- Informacast Fusion webhook client with OAuth2 client credentials.
- SQLite call history via aiosqlite.
- FastAPI web dashboard on port 8080 (dark theme, auto-refresh).
- Daily log rotation with .tgz compression, 90-day retention.
- systemd service with security hardening.
- Install/uninstall scripts.
