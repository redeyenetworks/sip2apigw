# sipgw Architecture

> **Release: v1.6.0 — reliability + observability.**
> This document reflects the shipped `release/v1.6.0` branch. v1.6.0 turns sipgw
> from a best-effort one-shot gateway into a **durable, record-first pager** with
> retry/backoff, escalation, real liveness reporting, a hardened test/dry-run
> substrate, and a decoupled read-only dashboard. Several items ship in a
> deliberately inert or shadow state and require human/host sign-off before they
> are switched on (see [Human/host-gated work](#humanhost-gated-work)).

## Overview

sipgw is a SIP-to-Webhook gateway that receives inbound SIP calls (Code Blue,
RRT, Code Pink alerts from a Rauland nurse-call system), parses the caller
information, builds a text-to-speech announcement string, and triggers an
Informacast Fusion scenario via REST API.

The load-bearing product requirement is that **a real Code Blue must never be
dropped, duplicated, suppressed, or misrouted**. Every reliability feature in
v1.6.0 is designed around that invariant: pages are persisted *before* any
network send (record-first), delivery is retried durably, and any de-duplication
logic ships observe-only.

## Two-service topology (v1.6.0, #14)

As of #14 the system runs as **two independent processes / systemd units**:

| Service                    | Unit                       | Process                        | DB access | Responsibility |
|----------------------------|----------------------------|--------------------------------|-----------|----------------|
| **Writer / gateway**       | `sipgw.service`            | `python -m sipgw.main`         | READ-WRITE (WAL) | SIP listener, record-first persistence, delivery worker, escalation, token refresh, heartbeat writer, systemd watchdog |
| **Dashboard**              | `sipgw-dashboard.service`  | `python -m sipgw.dashboard_app`| READ-ONLY (`query_only=ON`) | FastAPI web UI, CSV export, `/health` (reads the writer's heartbeat) |

The two processes share only the SQLite database file. WAL journaling lets the
read-only dashboard read consistently while the writer commits. The dashboard
opens the DB with `PRAGMA query_only=ON` so it can **never** mutate a page or the
heartbeat — the writer owns all writes. The single-service (rollback) topology
still works: the watchdog/notify code is inert without systemd, and the dashboard
process is optional for delivery (only observability is lost if it is down).

```
┌─────────────────────────────────────────────────────────────┐
│                     Rauland Nurse Call System                │
│                     (172.16.0.0/12 network)                  │
└──────────┬───────────────────────────┬──────────────────────┘
           │ SIP INVITE (UDP/TCP:5060)  │ RTP (discarded)
           ▼                            ▼
┌──────────────────────────────────────────────────────────────┐
│  sipgw.service  (WRITER — python -m sipgw.main)              │
│                                                              │
│  ┌──────────────┐   ┌───────────┐   ┌────────────────────┐  │
│  │  SIP Server   │──▶│  Parser    │──▶│  TTS Builder       │  │
│  │ (sip_server)  │   │ (parser)   │   │  (tts_builder)     │  │
│  │ UDP+TCP:5060  │   │ #15 invite │   └─────────┬──────────┘  │
│  └──────┬───────┘   │ fingerprint│              │             │
│         │            └───────────┘              ▼             │
│         │                          ┌────────────────────────┐│
│  on_call() RECORD-FIRST:           │ create_pending_call()  ││
│  persist PENDING row ──────────────▶  (state machine, WAL)  ││
│         │                          └───────────┬────────────┘│
│  #5 dedupe (SHADOW, after insert)              │ pending      │
│         │                                      ▼              │
│  ┌───────────────┐   poll/backoff   ┌────────────────────┐   │
│  │ Delivery      │◀─────────────────│  calls table       │   │
│  │ Worker (#2)   │─── deliver ──────▶  state transitions  │   │
│  │ retry/expire  │                  └────────────────────┘   │
│  └───┬───────┬───┘                                           │
│      │       │ failed/expired                                │
│      │       ▼                                               │
│      │  ┌──────────────┐   ┌──────────────┐                  │
│      │  │ Escalator #3 │   │ Heartbeat #7 │──▶ heartbeat row │
│      │  └──────┬───────┘   └──────────────┘                  │
│      ▼         ▼                                             │
│  ┌──────────────┐  ┌──────────────┐                          │
│  │Fusion Webhook│  │ Human channel│  #8 systemd Type=notify  │
│  │ OAuth2 +POST │  │(Teams/Slack) │  + watchdog pinger       │
│  │ #4 token bg  │  └──────────────┘                          │
│  └──────┬───────┘                                            │
└─────────┼────────────────────────────────────────────────────┘
          │                        shared SQLite (WAL)
          ▼                                 ▲ read-only
┌───────────────────────┐        ┌──────────┴───────────────────┐
│ Informacast Fusion    │        │ sipgw-dashboard.service       │
│ scenario-notifications│        │ (python -m sipgw.dashboard_app)│
└───────────────────────┘        │ HTTP:8080  UI + CSV + /health  │
                                 └───────────────────────────────┘
```

## Module Responsibilities

| Module           | File                | Purpose |
|------------------|---------------------|---------|
| **Config**       | `config.py`         | Load config.yaml into typed dataclasses; `validate_config` startup checks (#9) |
| **Lookups**      | `lookups.py`        | Area ID→name and purpose substitution tables |
| **Parser**       | `parser.py`         | Extract area/room/bed/purpose from the SIP From header |
| **TTS Builder**  | `tts_builder.py`    | Compose the announcement string |
| **SIP Message**  | `sip_message.py`    | Parse/build SIP + SDP; `invite_fingerprint` transaction identity (#15) |
| **SIP Server**   | `sip_server.py`     | UDP+TCP listener, call state machine, `on_call` dispatch |
| **RTP Handler**  | `rtp_handler.py`    | Send u-law silence RTP packets |
| **Safety**       | `safety.py`         | No-send guard transport, `[TEST]` marker, prod-DB barrier (§2) |
| **Webhook**      | `webhook.py`        | OAuth2 auth, background token refresh (#4), Fusion scenario trigger |
| **Database**     | `database.py`       | SQLite WAL store, delivery state machine, heartbeat, canonical time (#2/#7/#12) |
| **Delivery**     | `delivery.py`       | Durable delivery worker: retry/backoff, expiry, recovery, escalation hook (#2/#3) |
| **Escalation**   | `escalation.py`     | POST to a human channel on failed/expired pages (#3) |
| **Dedupe**       | `dedupe.py`         | Clinical fingerprint + SHADOW duplicate telemetry (#5) |
| **Watchdog**     | `watchdog.py`       | systemd `Type=notify` READY/WATCHDOG/STOPPING sd_notify (#8) |
| **Logging**      | `logging_config.py` | Async (QueueHandler) rotating+compressing file logging, dashboard-safe variant (#6/#11/#14) |
| **Dashboard**    | `dashboard.py`      | FastAPI UI, view toggle + CSV export (#13-P1), `/health` (#7) |
| **Dashboard App**| `dashboard_app.py`  | Decoupled read-only dashboard process entry point (#14) |
| **Main**         | `main.py`           | Writer entry point; wires SIP + delivery + heartbeat + watchdog |

## Call Flow (record-first, v1.6.0)

1. SIP INVITE arrives on port 5060 (UDP or TCP).
2. Source IP is checked against `allowed_networks` (default 172.16.0.0/12).
3. An **INVITE fingerprint (#15)** is computed for correlation/logging.
4. SIP server answers: `100 Trying`, then `200 OK` with SDP (PCMU/8000), and
   starts an RTP silence stream (0xFF u-law packets every 20ms).
5. `on_call()` runs the **record-first** path:
   - Parse the From header → `CallerInfo` (area, room, bed, purpose, display name).
   - Build the TTS string.
   - **`create_pending_call()` persists the page as a `pending` row** *before any
     network send*. This is the durability boundary — the page now survives a
     crash or Fusion outage.
6. **After** the insert, the clinical deduper (#5) runs as pure telemetry (see
   [#5](#5-clinical-dedupe--shadowdisabled)). It never gates or delays the insert
   or delivery.
7. The **delivery worker (#2)** picks up the pending row and POSTs to Fusion,
   retrying with backoff and escalating/expiring as needed.
8. The SIP call is held until BYE or timeout; on termination RTP is stopped and
   the call is cleaned up. **Delivery is fully decoupled from SIP call teardown.**

   In **`immediate_bye`** mode (production) the gateway does not hold RTP: it
   answers with `200 OK`, fires the page immediately (step 5, decoupled), and
   **defers the gateway BYE until the caller's ACK confirms the dialog** — see
   the deferred-BYE state machine below.

### #11 — Immediate-BYE ACK-gated teardown (deferred-BYE state machine)

Sending the gateway BYE in the same tick as the `200 OK` (the old behavior)
raced the caller's ACK: the BYE could reach the proxy before the three-way
handshake completed, drawing a **481 Call/Transaction Does Not Exist**. The fix
gates teardown on the ACK:

- On the `INVITE`, after the `200 OK`, the call is **kept** in `self.calls`, the
  page fires via `create_task(_safe_callback)` (never touches teardown), and a
  per-call **lost-ACK fallback** timer is armed
  (`immediate_bye_ack_timeout_seconds`, default 2.0s).
- Any of **{ACK arrives, fallback fires, peer BYE, shutdown}** funnels through
  one idempotent `_immediate_bye_teardown`, which flips `answered → terminating`
  with **no `await` between the check and the set** (atomic in the single-thread
  loop). The deferred BYE is therefore sent **exactly once** and the RTP port is
  freed exactly once; a lost ACK still tears down (fallback) and a duplicate ACK
  or an ACK/fallback race cannot double-send.
- The **durability contract** is *answer-SIP-first, deliver-async*: the page is
  recorded and dispatched independently of the ACK/BYE/fallback outcome, so a
  lost ACK or a teardown error can never lose a Code Blue.

The BYE is also made **spec-correct** (additive string-building only): the
request-URI targets the caller's captured **Contact** (falling back to
`From-user@remote` when the INVITE carried no Contact) and the **reversed
Record-Route** becomes the Route header set. Packet **routing is unchanged** —
the datagram is still transmitted to `call.remote_addr` (the INVITE source = the
adjacent Record-Route hop); only the request-URI / Route header *content* changes.

## Shipped work (v1.6.0)

### #2 — Durable, record-first delivery + retry/backoff

The core reliability change. `database.py` adds a **delivery state machine** on the
existing `calls` table (columns added idempotently via `ALTER TABLE ... ADD COLUMN`,
so the ~301 legacy prod rows migrate losslessly to `state='legacy'`, `attempts=0`,
`is_test=0`):

`pending → delivering → delivered` (success) or `→ failed` (exhausted) or
`→ expired` (too old). States: `pending | delivering | delivered | failed |
expired | legacy`.

- **Record-first is sacred.** `create_pending_call` inserts the `pending` row
  before any send; nothing (including dedupe) gates that insert.
- The `DeliveryWorker` (`delivery.py`) polls `get_deliverable()` oldest-first and
  for each row: expires it if `age > max_age_seconds`; otherwise respects an
  in-memory backoff cooldown, calls `mark_attempting` (increments `attempts`),
  and triggers the webhook. On 2xx → `mark_delivered`; on failure with
  `attempts >= max_attempts` → `mark_failed` + escalate; else → `reschedule`
  (back to `pending`) with exponential backoff `base_backoff * 2^(n-1)` capped at
  `max_backoff`, honoring a `Retry-After` delta-seconds header when present.
- **Crash recovery:** on startup `recover_inflight()` returns any orphaned
  `delivering` rows to `pending` (at-least-once delivery). In-memory cooldowns are
  intentionally lost on restart — recovery re-queues and we retry.
- `drain()` is a best-effort flush on graceful shutdown; durability does not
  depend on it (record-first + recover cover a hard stop).
- The worker never sends directly — it drives `FusionWebhook`, which carries the
  §2 no-send guard in dry-run.

Config (`DeliveryConfig`): `max_attempts=6`, `base_backoff_seconds=2.0`,
`max_backoff_seconds=60.0`, `max_age_seconds=900.0`, `poll_interval_seconds=1.0`,
`batch_size=20`.

### #3 — Escalation on failed/expired

`escalation.py` `Escalator` is injected into the worker as `on_escalate(reason,
row)`. It fires on `failed` (retries exhausted) and `expired` (stale), POSTing a
JSON payload to `escalation.webhook_url` (Teams/Slack/PagerDuty/NOC). If no URL is
configured the failure is still logged loudly at ERROR. Escalation is **robust by
contract**: any exception is logged, never raised, so it can never disrupt
delivery. In dry-run the escalation client is built with `NoSendGuardTransport`,
so the POST cannot reach a real host during testing.

### #4 — Background OAuth2 token refresh

`webhook.py` runs a background `_refresh_loop` that keeps a fresh token cached,
renewing ~`token_refresh_margin_seconds` (default 300s) before expiry, off the
page path. The on-demand `_get_token(min_remaining=60)` path still exists as a
fallback (and a `401` still triggers a clear-and-retry), but under normal
operation the first real Code Blue never pays a token-fetch latency.

### #5 — Clinical dedupe — SHADOW/DISABLED

Ships **inert**. The intent is to measure how often true duplicate pages arrive
without ever risking a suppressed Code Blue.

- `dedupe.py` computes a stable **clinical fingerprint** — the normalized tuple
  `(area, room, bed, purpose)` with leading zeros preserved — prefixed `cf-v1:`.
  This is deliberately **distinct** from #15's `v1:` INVITE transaction
  fingerprint; the two are never unified.
- `Deduper.evaluate` runs **after** the record-first insert (main.py `on_call`)
  and is **non-suppressing telemetry**: it may annotate `duplicate_of` and log,
  but it never skips or delays delivery. A real second Code Blue for the same
  room is always delivered.
- **Two OFF switches**, both defaulting off:
  1. `window_seconds = 0` (shipped default) → the DB is never even queried; the
     decision is fingerprint-only.
  2. `enforce = False` (shipped default) → even a windowed match only logs
     `WOULD suppress …` and still returns no-suppress.
- `enforce=True` is **forbidden in every mode**: `validate_config` makes it a
  fatal startup error. A test-only `window_seconds > 0` turns on the shadow
  `WOULD suppress` telemetry, and delivery still always proceeds.
- Enabling real suppression requires clinical sign-off and a real Rauland INVITE
  capture (see [Human/host-gated work](#humanhost-gated-work)).

### #6 — Async logging

`logging_config.py` attaches every real file handler through a
`QueueHandler` + background `QueueListener`, so a logging call from the event loop
only enqueues — all file writes, midnight rotation, and `.tgz` compression happen
on a background thread and never block delivery. `CompressingTimedRotatingFileHandler`
handles daily rotation, gzip-tar compression of rotated files, and retention purge.
Listeners are flushed at interpreter exit (`atexit`).

### #7 — Heartbeat + real `/health`

The writer stamps a `heartbeat` row (`write_heartbeat("writer")`) on startup and
every `heartbeat_interval_seconds` (default 10s). The dashboard's `/health`
(`read_heartbeat`) returns `200 {"status":"ok"}` only if the beat is fresher than
`stale_after_seconds` (default 30s); otherwise `503` with `stale` or
`no-heartbeat`. Because the writer and dashboard are separate processes, `/health`
reports true cross-process writer liveness, not just "the web server answered".

### #8 — systemd `Type=notify` watchdog

`watchdog.py` is a pure-Python `sd_notify` implementation. `main.py` sends
**`READY=1` before recovery** so a large `recover()` cannot delay READY and trip a
watchdog restart loop. The `WatchdogPinger` then pings `WATCHDOG=1` on
`WATCHDOG_USEC/2` cadence, proving **event-loop** liveness — decoupled from DB
writes, so transient DB slowness never restarts the life-safety pager. Everything
is structurally **inert when `NOTIFY_SOCKET` is unset** (tests, dry-run,
non-systemd, single-service rollback). `sipgw.service` sets `Type=notify`,
`NotifyAccess=main`, `WatchdogSec=30`.

### #9 — Startup config validation

`config.validate_config` raises `ConfigError` (exit code 2) on fatal problems and
returns non-fatal warnings. In production (dry-run off) it **requires** Fusion
credentials, `scenario_id`, and a **preset `scenario_field_id`** so the first real
page cannot fail auth or trigger a live field-id lookup. It also validates URLs,
SIP port/RTP ranges, `allowed_networks` CIDRs, delivery bounds, and makes
`dedupe.enforce=True` fatal. Both the writer and the dashboard run it at startup.

### #10 — State-aware, test-excluding stats

`get_today_stats` classifies today's **real** calls (`is_test=0`) by delivery
state: `success = delivered (+ legacy 2xx)`, `failed = failed + expired
(+ legacy non-2xx)`, `pending = pending + delivering`. Legacy rows predate the
state machine and are classified by their stored `fusion_status` for continuity
across the cutover boundary. Every dashboard/stat/export query filters
`is_test=0` so dry-run/test rows never appear in the live UI or an export.

### #11 — Logging hygiene (shipped halves)

BYE Via-transport correctness, credential masking (both `client_secret` **and**
`client_id` in form bodies, `Authorization: Bearer` truncation, access-token
masking in JSON responses), and `type(e).__name__` in exception logs. Autoescape,
masking, and the SIP IP allowlist are treated as non-negotiable safety surfaces.
The behavioral half of #11 — the ACK-gated deferred-BYE teardown that closes the
481 race — is documented under [Call Flow](#11--immediate-bye-ack-gated-teardown-deferred-bye-state-machine).

### #12 — Canonical UTC RFC3339-Z timestamps, host-local display

Stored `timestamp` is canonical **UTC RFC3339 millis-`Z`** (`_utc_rfc3339`).
Bucketing and day-boundary logic key off the numeric `created_at` **epoch**
(uniform across legacy-local and new-UTC rows) — never the string. Display
(`display_local`) and the day boundary (`_day_start_epoch`) resolve the timezone
from the **host** by default (`timezone = ""`/`host`/`local`/`system`), or an
explicit IANA name per install. The dashboard shows a `Time (local)` column;
nurses see local wall-clock derived from the canonical epoch.

### #13-P1 — Dashboard view toggle + CSV export

The dashboard has a **Summary ↔ Advanced** view toggle (invalid values fall back
to Summary, never a 500) and an **Export CSV** link. `/export.csv` streams today's
**real** calls (`db.export_calls` always appends `AND is_test=0`), quoted by the
stdlib `csv` module, filename `sipgw-calls-<YYYY-MM-DD-local>.csv`.

### #14 — Two-service split (read-only dashboard)

Covered in [Two-service topology](#two-service-topology-v160-14). Key safety
properties: the dashboard opens the DB **read-only** (`query_only=ON`, not
`mode=ro`, so the `-wal`/`-shm` sidecars can still build while logical writes are
blocked); the **prod-DB barrier runs on every open, including the read-only
reader**; and the dashboard uses `setup_dashboard_logging` — a console handler
plus its **own** `sipgw_dashboard.log`, never the writer's shared log files (two
processes racing midnight `doRollover()` would corrupt them).

### #15 — INVITE fingerprint

`sip_message.invite_fingerprint(msg)` is a stable **transaction-scoped** identity
keyed on `Call-ID + From user-part + From tag + CSeq`, prefixed `v1:`. A UDP
retransmission of the same INVITE yields the same fingerprint; a genuinely new
call differs. Via/branch and Contact are excluded so hop-by-hop routing changes do
not perturb it; values are stripped but not lowercased (case-sensitive per
RFC 3261). It is computed on the SIP handle path for correlation/logging and is
the basis for future #5 transaction-level work — **distinct** from #5's clinical
identity.

## Safety substrate (§2 — load-bearing, never weakened)

- **No real outbound send in dev/test.** In effective dry-run the shared httpx
  client is built with `NoSendGuardTransport`, which forwards only to `127.0.0.1`
  (local mock server) and refuses every other host, returning a synthetic
  response without touching the network. Every Fusion origin (`_get_token`,
  `_resolve_field_id`, `trigger_scenario`) **and** the escalation POST share this
  client, so the guarantee is structural, not per-call-site discipline. Dry-run
  can only be **ENABLED** (config flag or `SIPGW_DRY_RUN=1`), never disabled by
  env.
- **`[TEST]` marker + `is_test=1`.** In dry-run the `TestMarkerFilter` prefixes
  every physical log line (including multi-line SIP/API dumps) with `[TEST] `, and
  every persisted row is `is_test=1`.
- **Prod-DB hard barrier.** `assert_safe_database_path` runs on **every** DB open
  — writer and read-only reader alike — and aborts startup if dry-run/test is
  active while `database.path` resolves to `/var/lib/sipgw/calls.db`.
- **Jinja `autoescape=True`**, credential masking, and the SIP IP allowlist are
  never weakened.

## SIP Implementation

A purpose-built, lightweight SIP implementation (not a full stack) handling only
INVITE / ACK / BYE / CANCEL / OPTIONS. Chosen over pjsua2/sipsimple for zero
native dependencies, pure-Python install, and full behavioral control for the
gateway's narrow requirements.

## Security

- SIP accepted only from configured networks (default 172.16.0.0/12).
- Both units run as the unprivileged `sipgw` user under `ProtectSystem=strict`,
  `NoNewPrivileges`, `PrivateTmp`, with narrow `ReadWritePaths`. The writer holds
  `CAP_NET_BIND_SERVICE` for port 5060; the dashboard unit adds `MemoryMax=256M`
  and `CPUQuota=50%`.
- Config contains the OAuth2 secret — file permissions 640; secrets masked in
  logs.
- The dashboard has no authentication (internal-network use only) and is
  read-only at the database layer.

## Human/host-gated work

These items ship but must **not** be considered live until a human/host gate is
cleared:

- **Real-systemd watchdog + OOM isolation drills (#8/#14).** The notify/watchdog
  and `MemoryMax` behavior must be validated against real systemd (READY timing,
  watchdog restart, dashboard OOM isolation from the writer). Not exercised by the
  unit suite (inert without `NOTIFY_SOCKET`).
- **#5 dedupe enforcement.** Real suppression stays forbidden
  (`validate_config` fatal on `enforce=True`) pending **clinical sign-off** and a
  **real Rauland INVITE capture** to validate the clinical-fingerprint field
  derivation against production traffic. Until then it is shadow/telemetry only.
- **Cutover.** Migrating the running prod install to the two-service topology
  follows the cutover runbook (`docs/RUNBOOK-cutover-2026-07-01.md`), preserving
  `lookups.yaml`/`config.yaml` and the existing `calls.db` with no data loss.

## Testing

Run the full suite (266 tests pass as of this release):

```
cd /home/sipgw/sipgw-work
SIPGW_LOOKUPS=/home/sipgw/sipgw-work/lookups.yaml ./venv/bin/python -m pytest -q
```
