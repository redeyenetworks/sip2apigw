# Changelog

All notable changes to the sipgw project are documented in this file.

---

## [Unreleased] — v1.7 (writer)

Behavioral SIP change — **writer restart** (brief ~0.3s SIP blip); no dashboard impact.

- **#11 immediate-BYE is now ACK-gated (closes the 481 race).** In `immediate_bye`
  mode the gateway used to send its BYE in the same tick as the `200 OK`, which
  could outrun the caller's ACK and draw a **481 Call/Transaction Does Not Exist**.
  The gateway now answers, keeps the call, **fires the page immediately (fully
  decoupled from teardown)**, and defers the gateway BYE until the ACK confirms the
  dialog. A new per-call **lost-ACK fallback** (`sip.immediate_bye_ack_timeout_seconds`,
  default 2.0s) tears the dialog down and frees the RTP port if the ACK is lost, so a
  dropped ACK can never strand the dialog or leak a port. The teardown funnel is
  single-fire (atomic `answered → terminating`), so a duplicate ACK or an ACK/fallback
  race can never double-send the BYE or double-free the port. A retransmitted INVITE
  during the ACK-wait now hits the re-INVITE 200-resend branch instead of re-paging.
- **#11 spec-correct BYE (additive).** The BYE request-URI now targets the caller's
  **Contact** (falling back to `From-user@remote` when absent) and carries the
  **reversed Record-Route** as its Route set. **Packet routing is unchanged** — the
  datagram still goes to `remote_addr`; only the request-URI / Route header *content*
  changed.
- New knob `sip.immediate_bye_ack_timeout_seconds` (default 2.0). Staging now mirrors
  production (`immediate_bye: true`) and a new **host drill M7** gates the deploy:
  INVITE→200→ACK→BYE with zero 481 / zero "ACK for unknown call", plus the lost-ACK
  fallback path.
- **#5 clinical dedupe SUPPRESSION enabled (closes #5).** After clinical sign-off,
  `dedupe.enforce` is no longer a fatal config — it is enabled in production with the
  **safest** parameters: `window_seconds: 2` (only near-instant duplicates; a re-page
  >2s later is treated as legitimate and always delivered), **bed-level** (`match_bed:
  true`, so two patients coding in one room are never merged), and purpose in the key
  (RRT and Code Blue never merge). A suppressed page is **still recorded** (record-first:
  it becomes a durable `duplicate` audit row pointing at the original), it is simply not
  delivered. Suppression is **fail-safe**: it is guarded on `state='pending'`, so if the
  delivery worker has already picked the page up, the page is delivered rather than
  dropped — every race resolves toward *delivery*, never a wrongful drop. `validate_config`
  now WARNS loudly (`*** DEDUPE SUPPRESSION ACTIVE ***`) instead of refusing to start, and
  flags an inert (`window<=0`) or wide (`window>10s`) window. **Kill-switch:**
  `dedupe.enforce=false` + restart. A new host drill **M8** gates the deploy (two same-bed
  pages <2s → one suppressed; >2s → both delivered).

## [v1.6.5] — 2026-07-03

Dashboard-only (zero SIP impact).

- **#13 date picker is now a most-recent-first dropdown.** Replaced the native date
  input with a `<select>` whose options are **descending** (Today first, then
  yesterday, …), so looking back a day or two is at the top of the list rather than
  starting months ago. Works consistently across browsers. Today and the current
  selection are always present.

## [v1.6.4] — 2026-07-03

Dashboard-only (zero SIP impact) — date-picker UX.

- **#13 the date picker now defaults to the current date** (was: the last day that
  happened to have logs, which could show a stale month) and moved to a **prominent
  labelled bar at the top** of the page (📅 "Viewing date (zone)" + a Today/Historical
  badge + "Back to today"), since it drives the whole page (call table + stats + logs),
  not just the log panel. The redundant in-log picker was removed. Today/live keeps the
  existing today-only query; a historical pick uses the local-day window.

## [v1.6.3] — 2026-07-03

Backlog burn-down batch. Dashboard parts deploy with **zero SIP impact** (restart
only `sipgw-dashboard`); the two writer parts activate on a coordinated writer
restart (additive/idempotent, safe even on an unplanned one).

### Dashboard (zero SIP impact)
- **#7 opt-in Fusion-unreachable /health signal (closes #7).** New
  `health.fail_on_fusion_unreachable` (default **false**) + `fusion_unreachable_max_age_seconds`;
  when off (default), `/health` behavior is byte-for-byte unchanged (status stays
  heartbeat-only). When an operator opts in, a *stale/failed* Fusion probe can degrade
  `/health` — never a transient blip.
- **#13 F1 — the date picker now also drives the call table + stats**, not just the log
  viewer: selecting a day filters the table/stats/logs to that **local** day (one
  `_local_day_window` source of truth).
- **#13 F2 — 90-day stacked "calls by type" chart.** A self-contained inline SVG stacked
  bar chart over the last 90 local days, stacked by call **type/purpose** (Code Blue /
  RRT / Code Pink / any future type — derived from `display_name` via lookups, so all
  legacy rows count). No external JS/CDN; excludes `is_test=1`.
- **#13 Phase-2 — `/call/{id}` correlated call-detail view.** Joins the call row with its
  exact SIP + main-log lines by Call-ID (read-only, autoescaped; test rows 404 to keep
  the UI-wide `is_test=0` discipline).

### Writer (coordinated restart)
- **#15 persist the upstream `event_id`.** Idempotent nullable `event_id` column + index
  (legacy rows stay NULL), threaded from the INVITE through record-first
  `create_pending_call` and surfaced read-only on the dashboard. The extraction shipped
  in v1.6.1; this stores it (and is the merge-key prerequisite for #17 HA and #5's
  event-id dedupe).
- **Inbound-liveness / Rauland-reachability monitor (new).** An additive stamp of the
  last inbound SIP receive time (after the IP allowlist — no behavior change to
  INVITE/ACK/BYE), persisted like the heartbeat and surfaced as `last_inbound_sip_age_s`
  in `/health` + a dashboard card. Answers "is the Rauland link up, or just quiet?".
  Optional silence escalation is **default OFF** (`inbound_escalate_after_seconds=0`);
  the age never flips the `/health` status code.

## [v1.6.2] — 2026-07-03

Dashboard-only (zero SIP-path impact).

- **#13 timezone-aware date-picker log viewer.** The log viewer gains a date
  picker across all three streams. "A day" is the **viewer's day**, defined in the
  configured display zone (`logging.timezone`, labelled in the UI). Because logs
  are UTC-stamped and rotate at UTC midnight, a local day is gathered across the
  overlapping UTC archive(s) and filtered by each entry's timestamp (DST-correct;
  multi-line entries kept as a unit). Avoids re-decompressing: the day list is
  built from archive filenames, and reads decompress at most the 1–2 relevant
  archives, cached by `(path, mtime)`. UTC rotation is unchanged.

## [v1.6.1] — 2026-07-02

Backlog burn-down: a batch of **safe, additive, outage-free** observability + diagnostics
improvements (no SIP/delivery behavior change, no dedupe enforcement). Closes **#9** and
**#12**; advances **#5, #7, #11, #13, #15**.

- **#12 log timestamps → UTC RFC3339 millis-Z.** All log streams (`sipgw.log`,
  `sipgw_api_debug.log`, `sipgw_sip_debug.log`, and the dashboard's own
  `sipgw_dashboard.log`) now emit a canonical `YYYY-MM-DDTHH:MM:SS.mmmZ` stamp
  via the new `ISO8601Formatter`, replacing the ambiguous space-separated
  host-local `YYYY-MM-DD HH:MM:SS`. All streams are byte-for-byte zone-consistent,
  UTC-sortable, and string-matchable against the Singlewire `Date`/`createdAt`
  fields. This completes the log half of #12 (the DB/dashboard half shipped in
  v1.6.0).
- The dashboard and CSV export are unchanged — they still render **host-local**
  wall-clock and compute the "today" boundary from `logging.timezone`. Log stamps
  are hard-coded UTC-Z and are **not** driven by that knob.
- The host runs UTC, so the #6 `when="midnight"` rotation continues to roll at
  00:00 UTC; each day-file and the UTC-Z stamps inside it now share the same UTC
  calendar day (self-consistent). No rotation/compression/retention behavior
  changed. No SIP/delivery/call-path change — purely additive observability.
- Operators: log scrapers expecting the old local stamp must adjust to `...T...Z`.
- **#9 unknown-key startup warnings (final acceptance criterion).** Misspelled
  config keys (e.g. `sip.imediate_bye`) and unknown top-level sections are now
  surfaced as **non-fatal** startup warnings naming the offending key
  (`unknown key 'sip.imediate_bye' ignored (typo?)`) instead of being silently
  dropped. Unknown keys are still **not applied** (the real field keeps its
  default), and this is warning-only — an unexpected/forward-compat key never
  refuses to start prod. Warnings ride the existing `validate_config` warning
  loop, so both the gateway and the dashboard log them with no caller change.
  Completes #9 (the fatal-validation half shipped in v1.6.0).
- **#11 full credential redaction (closes the masking sub-item).** `client_secret`
  and `client_id` are now **fully** redacted in `api_debug` request logs (the prior
  4-char prefix leak is gone). The BYE-before-ACK (481) teardown items remain open
  as a separate SIP-state-machine change.
- **#15 structured INVITE fingerprint line + upstream event-id extraction.** Each
  inbound INVITE now logs a structured, greppable line carrying the retransmit-stable
  transaction `fp=` plus a best-effort upstream `event_id` (Call-ID segment) — purely
  additive telemetry, never touching routing/answer/BYE or the `on_call` callback.
- **#5 richer SHADOW dedupe audit trail.** The (still-disabled) shadow detector's
  `WOULD suppress` line now records the inter-page `gap`, `bed_match`/`purpose_match`,
  and **both** Call-IDs, so the live duplicate-rate/gap evidence needed for the
  clinical window decision is captured. Enforcement stays `validate_config`-forbidden;
  a real second Code Blue is still always delivered.
- **#7 enriched `/health` + additive Fusion reachability keepalive.** `/health` gains
  informational fields (`backlog`, `last_delivered_at`, `fusion_reachable`,
  `fusion_checked_age_s`); the **200/503 status stays driven solely by the writer
  heartbeat** (no new failure flips it, so external monitors are unaffected). A
  writer-side keepalive stamps Fusion reachability off the page path and is routed
  through the no-send guard in dry-run (reaches no real host).
- **#13 dashboard v2 Phase-1 remainder.** Time toggle (Local/UTC/Both), plain-language
  + glyph + `aria-label` delivery status (WCAG, no colour-only signalling), a friendly
  `Fusion Result` CSV column + UTC timestamp, and a read-only `get_calls_between` range
  export. Dashboard-only (isolated read-only process); the epic's Phases 2–5 stay open.

## [v1.6.0] — 2026-07-01

Reliability + observability release. The gateway moves from best-effort,
single-process paging to a **durable, record-first delivery pipeline** with retry,
escalation, and a decoupled read-only dashboard. Every change here preserves the
life-safety invariants: no real outbound send in dev/test (`NoSendGuardTransport`),
`[TEST]`-marked logs and `is_test=1` rows under dry-run, the prod-DB path barrier on
every DB open, and — above all — **a real page is never dropped, duplicated, or gated
by any new machinery.**

**Validated in production (2026-07-01).** Cut over on the live host after all six
real-systemd drills passed there (Type=notify `READY`, watchdog kill+restart, dashboard
crash isolation, WAL `-shm` under `ProtectSystem=strict`, two-process no-send,
restart-recovery). Confirmed on a live Code Blue — call #303: INVITE → fingerprint
`v1:c621e265…` → record-first `PENDING` row → Fusion **HTTP 200 in 795 ms** → overhead
page fanned to **12 IP speakers**, delivered on attempt 1, `is_test=0`, no duplicate,
no escalation. `lookups.yaml` preserved byte-identical and all 302 prior rows migrated
losslessly.

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

### Deferred to future releases (tracked as open issues)
v1.6.0 ships some issues partially by design; the remainder stays tracked and open:
- **#5 dedupe enforcement** stays SHADOW/DISABLED (suppression is
  `validate_config`-forbidden) until clinical sign-off **and** a real Rauland INVITE
  capture validate the clinical fingerprint; adopting the **upstream event-id** as the
  primary key is part of that follow-up.
- **#7 Fusion-reachability keepalive probe** — `/health` reports the writer heartbeat
  (dead-writer detection); an active Fusion-reachability / component-level probe is
  not yet implemented.
- **#12 log-line timezone offset** — stored DB timestamps and dashboard display are now
  unambiguous (UTC stored, host-local shown), but the log **formatters**
  (`logging_config.py`) still emit local time without an explicit offset.
- **#15 upstream event-id extraction** — the retransmit-stable transaction fingerprint
  shipped; parsing an upstream event-id header for cross-system correlation is open.
- **#11 BYE-before-ACK (481) teardown race**, **#9** residual config-validation
  coverage, and **#13** dashboard Phases 2–4 remain open with exact scope documented on
  each issue.
- **#17 Shared-nothing HA** (active/active, fail-open) is a separate future epic.

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
