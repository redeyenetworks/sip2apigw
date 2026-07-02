# Configuration Reference (v1.6.0)

All configuration is in `/opt/sipgw/config.yaml`. Lookup tables are in
`/opt/sipgw/lookups.yaml`. A fully commented template lives in
`config.yaml.example`.

> **v1.6.0 is a reliability + observability release.** The system is now a
> **two-service split** (#14): a life-safety **writer** (`sipgw.service`) and a
> read-only **dashboard** (`sipgw-dashboard.service`). **Both processes load the
> same `config.yaml`.** The writer consumes `sip`, `delivery`, `escalation`,
> `dedupe`; the dashboard consumes `dashboard` and `health`; `fusion`,
> `logging`, `database`, and `tts` are used by both. See
> [Two-Service Topology](#two-service-topology-14) below.

---

## Safety model (read first)

These invariants hold regardless of configuration and are enforced in code:

- **No-send guard (dry-run).** Effective dry-run = `fusion.dry_run: true` **OR**
  env `SIPGW_DRY_RUN=1`. The environment can only *enable* dry-run, never
  disable it. In dry-run every outbound HTTP client (the Fusion webhook **and**
  the #3 escalation client) is built with `NoSendGuardTransport`
  (`sipgw/safety.py`), so no notification can reach a real host.
- **Production-DB barrier.** `assert_safe_database_path()` runs on **every** DB
  open — including the dashboard's read-only open. If dry-run is active and
  `database.path` resolves to the production DB, startup aborts. Staging must set
  a staging-only `database.path`.
- **Test marking.** In dry-run every log line carries a `[TEST]` marker and every
  DB row is written `is_test=1`. The dashboard's live views and CSV export
  hard-filter `is_test=0`, so a test row can never appear as a real page.
- **Record-first is sacred (#2).** The page is persisted `pending` *before*
  anything is sent. The #5 deduper runs **after** the insert and never gates it.
- **Never drop a real second Code Blue (#5).** Dedupe ships SHADOW/DISABLED;
  suppression is forbidden (see [Dedupe](#dedupe-section-5--shadowdisabled)).

---

## config.yaml

### SIP Section (writer)

```yaml
sip:
  bind_ip: "0.0.0.0"               # IP to bind SIP listener (0.0.0.0 = all interfaces)
  bind_port: 5060                  # SIP port (standard = 5060)
  allowed_networks:                # Source IP allowlist (CIDR). Empty = reject ALL sources.
    - "172.16.0.0/12"
    - "127.0.0.0/8"
    - "10.0.0.0/8"
  call_timeout_seconds: 600        # Max call duration before auto-hangup (10 min)
  immediate_bye: true              # Hang up as soon as the page is recorded
  rtp_port_range_start: 10000      # Start of RTP port range (even numbers)
  rtp_port_range_end: 20000        # End of RTP port range
```

`allowed_networks` is a load-bearing security control — do not widen it. Invalid
CIDR entries are a **fatal** config error (#9). An empty list is a warning and
rejects all SIP sources.

> **Misspelled or unknown keys** (e.g. `sip.imediate_bye`) and unknown top-level
> sections are logged as **non-fatal** startup warnings naming the offending key
> and are ignored (the real field keeps its default) — check the startup log for
> `config: unknown key '…' ignored (typo?)` if a setting seems to have no effect (#9).

### Fusion Section (both services; credentials used by the writer)

```yaml
fusion:
  base_url: "https://api.icmobile.singlewire.com/api"
  token_url: "https://api.icmobile.singlewire.com/api/token"
  audience: "YOUR_PROVIDER_ID"
  scenario_id: "YOUR_SCENARIO_ID"
  scenario_endpoint: "/v1/scenario-notifications"
  variable_name: "customTTS"       # JSON key carrying the TTS text
  scenario_field_id: ""            # PRESET this in production (see below)
  client_id: "YOUR_CLIENT_ID"
  client_secret: "YOUR_CLIENT_SECRET"   # <-- SET THIS
  token_refresh_margin_seconds: 300     # #4 refresh the OAuth2 token this early
  dry_run: false                        # true = no real sends (see Safety model)
```

- **#4 background token refresh.** A background task keeps a fresh OAuth2 token
  cached, renewing `token_refresh_margin_seconds` before expiry, so a live Code
  Blue never blocks on a token round-trip.
- **`scenario_field_id`.** In production this **must be preset** (fatal if empty)
  so the first real page does not trigger a live field-id lookup.

### TTS Section (both services)

```yaml
tts:
  play_count: 3                    # Times the message is repeated in the scenario
  message_preamble: "Attention! Attention! "   # Prepended once
  iteration_preamble: ""           # Prepended to each repeat
```

### Delivery Section (#2, writer)

Tuning for the durable record-first delivery worker. Pages are recorded
`pending` on the SIP path, then delivered asynchronously with exponential
backoff so a Fusion outage or a crash between record and send cannot drop a page.

```yaml
delivery:
  max_attempts: 6                  # Attempts before a page is 'failed' + escalated
  base_backoff_seconds: 2.0        # Exponential backoff base
  max_backoff_seconds: 60.0        # Backoff cap (also caps honored Retry-After)
  max_age_seconds: 900.0           # Undelivered longer than this -> 'expired' + escalate
  poll_interval_seconds: 1.0       # Worker poll cadence (must be > 0)
  batch_size: 20                   # Rows processed per pass
```

- Backoff honors a Fusion `Retry-After` delta-seconds header, capped by
  `max_backoff_seconds`.
- On startup, crash-orphaned `delivering` rows are recovered to `pending`
  (at-least-once delivery).
- `max_attempts < 1` and `poll_interval_seconds <= 0` are **fatal** (#9).

### Escalation Section (#3, writer)

When a page reaches `failed` (attempts exhausted) or `expired` (too old), a JSON
alert is posted to a human channel (Teams / Slack / PagerDuty / NOC webhook).

```yaml
escalation:
  webhook_url: ""                  # Empty = escalation disabled (still logged at ERROR)
  timeout_seconds: 10.0
```

- Empty `webhook_url` disables escalation; failures/expiries are still logged
  loudly at ERROR. In production an empty URL is a **warning** (#9).
- In dry-run the escalation client carries the no-send guard, so this URL is
  blocked during testing.
- Escalation errors are logged, never raised — they never disrupt delivery.

### Health Section (#7, dashboard)

Single source of truth for liveness. The writer stamps a heartbeat every
`heartbeat_interval_seconds`; the dashboard's `/health` returns **503** once the
heartbeat is older than `stale_after_seconds`.

```yaml
health:
  heartbeat_interval_seconds: 10.0
  stale_after_seconds: 30.0
```

`/health` responses: `200 {"status":"ok",...}`,
`503 {"status":"stale",...}` (heartbeat too old), or
`503 {"status":"no-heartbeat"}` (never stamped — writer down).

### Dedupe Section (#5 — SHADOW/DISABLED)

**Clinical dedupe ships inert and must stay that way.** It computes a *clinical*
identity for a page (area, room, bed, purpose) purely to measure how often true
duplicates arrive. It is deliberately distinct from #15's SIP transaction
`invite_fingerprint`.

```yaml
dedupe:
  enforce: false                   # OFF switch #1 — suppression. FATAL if true.
  window_seconds: 0                # OFF switch #2 — 0 = shadow lookup never runs.
  match_bed: true                  # Fields used by the (shadow-only) match
  match_purpose: true
```

**The two OFF switches and the rule:**

- `window_seconds: 0` (default) — the shadow duplicate lookup **never even
  queries the DB**; `evaluate()` returns a no-suppress, fingerprint-only
  decision.
- `window_seconds > 0` — turns on **shadow telemetry only**: a match is logged as
  `WOULD suppress ...` but the page is **still delivered**. Test/measurement use.
- `enforce: true` — would set `suppress=True`, but this is a **fatal config
  error** (`validate_config` rejects it in *all* modes). Suppression requires
  clinical sign-off and is **not approved**. Even if it were set, `main.py` never
  gates delivery on the decision.

**Never-drop rule:** a real second Code Blue for the same room **must be
delivered**. Dedupe runs *after* the record-first insert as non-suppressing
telemetry (it may annotate `duplicate_of` and log), never before it, and never
skips or delays the pending row.

### Logging Section (#6, #11, #12; both services)

```yaml
logging:
  log_dir: "/var/log/sipgw"        # Log file directory
  retention_days: 90               # Days to keep rotated (.tgz) logs
  rotation_time: "midnight"        # When to rotate
  timezone: ""                     # #12: "" = host local tz; IANA name to override
  api_debug_log: true              # Write sipgw_api_debug.log
  sip_debug_log: true              # Write sipgw_sip_debug.log
```

- **#6 async logging.** File writes, rotation, and `.tgz` compression run on a
  background `QueueListener` thread (via `QueueHandler`), so the event loop never
  blocks on disk I/O or a midnight rollover.
- The **writer** attaches `CompressingTimedRotatingFileHandler` to `sipgw.log`,
  and (when enabled) `sipgw_api_debug.log` and `sipgw_sip_debug.log`.
- **#11 hygiene.** Credential/secret masking in API-debug logs is preserved.
- **#12 timezone.** `timezone` controls the dashboard's local wall-clock display
  and the "today" day-boundary. Stored timestamps are always canonical UTC (see
  [Timestamps](#timestamps-12)). `""` reads the host local tz (hosts run UTC).
- **#12 log stamps (v1.6.1).** Every log line in all three streams (`sipgw.log`,
  `sipgw_api_debug.log`, `sipgw_sip_debug.log`, plus the dashboard's own
  `sipgw_dashboard.log`) is now stamped in **canonical UTC RFC3339 milliseconds-Z**
  (e.g. `2026-07-01T18:23:45.007Z`), identical across streams and string-matchable
  against the Singlewire `Date`/`createdAt` fields for free far-end correlation.
  This is **hard-coded UTC-Z and not governed by `logging.timezone`** — that knob
  only affects dashboard/CSV display and the day-boundary. To read a log stamp in
  Eastern, subtract 4h (EDT) or 5h (EST). Because the host runs UTC, the #6
  `when="midnight"` rotation rolls at 00:00 UTC (~20:00 ET), so each day-file and
  the UTC-Z stamps inside it share the same UTC calendar day. Operators grepping
  for the old space-separated `YYYY-MM-DD HH:MM:SS` local stamp must switch to the
  `...T...Z` form.

### Dashboard Section (#14, #13; dashboard process)

```yaml
dashboard:
  port: 8080                       # HTTP port for the read-only web dashboard
  bind_ip: "0.0.0.0"               # Dashboard bind IP (unprivileged port; no CAP_NET_BIND_SERVICE)
  auto_refresh_seconds: 30         # Page auto-refresh interval
  page_size: 20                    # Rows per page
```

- **#13-P1 view toggle + CSV export.** The UI toggles between a **Summary** and
  **Advanced** view (`?view=summary|advanced`; an invalid value falls back to
  summary, never a 500). `GET /export.csv` streams **today's real calls only**
  (`is_test=0` enforced in the query); rows are quoted by the stdlib CSV module.
- The dashboard opens the DB **read-only** (see topology below) and never writes.

### Database Section (both services)

```yaml
database:
  path: "/var/lib/sipgw/calls.db"  # SQLite database file path
```

- The writer opens the DB in **WAL** mode so the read-only dashboard can read
  concurrently. The dashboard opens the same file with `PRAGMA query_only=ON`
  and skips all writes/migrations.
- The production-DB barrier runs on every open (including the read-only one).
- A missing/empty `database.path` is **fatal** (#9).

---

## Two-Service Topology (#14)

v1.6.0 splits the single process into two systemd units, both loading the same
`config.yaml`:

| Service | Unit | Role | DB access | Logs to |
|---|---|---|---|---|
| Writer | `sipgw.service` | SIP listener + delivery worker (#2) + escalation (#3) + heartbeat (#7) + watchdog (#8) — owns **all** writes | read/write, WAL | `sipgw.log`, `sipgw_api_debug.log`, `sipgw_sip_debug.log` |
| Dashboard | `sipgw-dashboard.service` | Read-only web UI + `/health` | **read-only** (`query_only=ON`) | its **own** `sipgw_dashboard.log` |

- Entry points: `python -m sipgw.main <config>` (writer) and
  `python -m sipgw.dashboard_app <config>` (dashboard).
- The dashboard uses **dashboard-safe logging** (`setup_dashboard_logging`) and
  never attaches the rotating handler to the writer's shared log files — two
  processes racing a midnight `doRollover()` would corrupt them.
- The dashboard reads the writer's heartbeat row for `/health` (a plain SELECT,
  fine under `query_only`). **Both services must run for a healthy system.**
- The dashboard bootstrap mirrors the writer's: `load_config` →
  `effective_dry_run` → prod-DB barrier → `validate_config`, so a misconfigured
  or unsafe dashboard refuses to start exactly like the writer.

### Watchdog (#8, writer only)

`sipgw.service` is `Type=notify` with `WatchdogSec=30`. The app sends `READY=1`
once listeners are up (before recovery, so a large `recover()` never delays
READY), then pings `WATCHDOG=1` at `WATCHDOG_USEC/2`. Pings prove **event-loop**
liveness only, decoupled from DB writes, so transient DB slowness never restarts
the pager. The integration is pure-python sd_notify and is **inert without
`NOTIFY_SOCKET`** (tests, dry-run, non-systemd, and the single-service rollback
all behave as before). The dashboard is a plain `Type=simple` service (no
watchdog). It also sets `MemoryMax=256M` / `CPUQuota=50%` so a runaway UI request
cannot starve the writer (these enforce only under real systemd with the cgroup
controllers present).

---

## Timestamps (#12)

- Stored call/heartbeat times are **canonical UTC RFC3339 milliseconds-Z**.
- The dashboard displays **local wall-clock** ("Time (local)") and computes the
  "today" boundary using `logging.timezone`.
- Bucketing/classification keys off the numeric `created_at` epoch, so legacy
  local-format rows and new UTC rows both classify correctly.

## State-aware stats (#10)

Dashboard "today" stats and counts are computed from the #2 delivery **state**
and **exclude test rows** (`is_test=0`):

- `success` = `delivered` (+ legacy rows with a 2xx `fusion_status`)
- `failed` = `failed` + `expired` (+ legacy rows with a non-2xx `fusion_status`)
- `pending` = `pending` + `delivering`

Legacy rows predate the state machine and are classified by stored
`fusion_status` for continuity across the cutover boundary.

## INVITE fingerprint (#15)

`invite_fingerprint(msg)` (`sip_message.py`) computes a stable SIP **transaction**
identity — `v1:<16-hex>` keyed on Call-ID + From user-part + From tag + CSeq — so
a UDP retransmission of the *same* INVITE is recognizable. This is intentionally
**distinct** from #5's clinical `cf-v1:` fingerprint (who/where/why); the two are
separate functions and must never be conflated.

---

## lookups.yaml

### Area Mappings

Maps numeric area IDs from the SIP username to speech-ready location names. Uses
phonetic spelling for TTS clarity.

```yaml
areas:
  710: "3rd Floor. Cardiac Step-Down."
  730: "1st Floor. E.D."
  # Add new areas here...

default_area: "Unknown Area."
```

### Call Purpose Substitutions

Maps keywords found in the SIP display name to spoken announcements.

```yaml
call_purposes:
  "Blue": "Code Blue"
  "RRT": "Rapid Response Team"
  "Pink": "Code Pink"

default_purpose: "Code"
```

**Order matters**: first matching keyword wins.

Leading zeros in area/room/bed numbers are clinically significant and are
preserved (v1.4); '007' is not '7'.

---

## Environment Variables

| Variable         | Default                    | Purpose                                             |
|------------------|----------------------------|-----------------------------------------------------|
| `SIPGW_CONFIG`   | `/opt/sipgw/config.yaml`   | Override config file path                           |
| `SIPGW_LOOKUPS`  | `/opt/sipgw/lookups.yaml`  | Override lookups file path                          |
| `SIPGW_DRY_RUN`  | *(unset)*                  | `1` **enables** dry-run (no real sends). Cannot disable it. |

---

## Startup validation (#9)

Both services call `validate_config()` at startup and **refuse to start** on
fatal problems (exit code 2). Warnings are logged and startup continues.

**Fatal examples:** non-URL `fusion.base_url`/`token_url`; missing
`client_id`/`client_secret`/`audience`/`scenario_id`/`scenario_field_id` in
production; invalid `allowed_networks` CIDR; bad SIP/RTP/dashboard ports;
`delivery.max_attempts < 1` or `poll_interval_seconds <= 0`; **`dedupe.enforce:
true`**; missing `database.path`.

**Warning examples:** empty `allowed_networks`; empty `escalation.webhook_url` in
production; `call_timeout_seconds`/`delivery.max_age_seconds <= 0`.

---

## Applying Changes

- **config.yaml / lookups.yaml changes**: restart the affected service(s):
  `systemctl restart sipgw` (writer) and/or
  `systemctl restart sipgw-dashboard` (dashboard). No code changes are needed for
  table updates.
- Changing `sip`/`delivery`/`escalation`/`dedupe`/`fusion` → restart the writer.
- Changing `dashboard`/`health` → restart the dashboard. Shared sections
  (`fusion`/`logging`/`database`/`tts`) → restart both.

---

## Human / host-gated items (NOT yet validated in this release)

These require a real host or clinical sign-off and are **out of scope for the
code-level release**:

- **Real-systemd watchdog + OOM isolation drills.** The `Type=notify` watchdog
  and the dashboard's `MemoryMax`/`CPUQuota` isolation enforce only under real
  systemd with cgroup controllers; they must be drilled on the actual host.
- **#5 dedupe enforcement** needs **clinical sign-off** and a **real Rauland
  INVITE capture** before `enforce`/`window_seconds` could ever be reconsidered.
  It stays SHADOW/DISABLED until then.
- **Cutover** from the single-service topology to the two-service split is a
  host-gated operational step (see `docs/RUNBOOK-cutover-2026-07-01.md`).
