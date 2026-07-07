# As-Built — Current Production Deployment

_Snapshot captured **2026-07-07 19:23 UTC** from host **`sip2apibridge`** by the read-only `product-docs` host inventory. Describes the deployed build **`c23f3eb`** (branch `main`) — i.e. **v1.6.5 + 6 commits, the in-progress v1.7 line**. The deployed code matches GitHub `main` HEAD; the working tree carries only untracked backup files (no code divergence)._

## Host & platform

| Attribute | Value |
|---|---|
| Hostname | `sip2apibridge` |
| OS | Ubuntu 24.04.4 LTS |
| Kernel | Linux 6.8.0-124-generic (x86_64) |
| CPU | Intel Xeon Platinum 8558P — 4 vCPU (1 socket × 4 cores, 1 thread/core) |
| Memory | 15 GiB (~1.2 GiB used at capture) + 4 GiB swap |
| Storage | root `/` 38 GB (22% used); dedicated `/var` LV 32 GB (3% used) |
| Timezone | **Etc/UTC** (NTP-synced) |
| Uptime | ~3 weeks, 2 days at capture |

## Network

- Single interface **`ens34` → `10.249.0.60/24`**; default route `10.249.0.1`.
- `10.249.0.60` is the address the Rauland nurse-call system targets for SIP.

## Deployed build

| Attribute | Value |
|---|---|
| Commit | **`c23f3eb`** (2026-07-03) — `feat(#13): F3 — group Today view + real last-call-from-Rauland lookback` |
| Branch | `main` (= GitHub `main` HEAD — no local code divergence) |
| Relative to releases | **v1.6.5 + 6 commits** (the unreleased **v1.7** line) |
| Working tree | untracked backups only: `config.yaml.pre-v1.7.*`, `lookups.yaml.bkp`, `fixesprompt.md` — no tracked-file modifications |
| Python | 3.12.3 (venv `/opt/sipgw/venv`) |

**Key package versions:** fastapi 0.129.0 · starlette 0.52.1 · uvicorn 0.41.0 · httpx 0.28.1 · httpcore 1.0.9 · aiosqlite 0.22.1 · PyYAML 6.0.3 · Jinja2 3.1.6 · anyio 4.12.1.

## Services — two-service topology

Two independent systemd units, both **active & enabled** (the call path is isolated from the dashboard):

| Unit | `Type` | `Restart` | `WatchdogSec` | Listens | Role |
|---|---|---|---|---|---|
| `sipgw.service` | **notify** | always (5s) | **30s** | 5060/udp+tcp | Call path — SIP + durable delivery |
| `sipgw-dashboard.service` | simple | always (5s) | — | 8080/tcp | Reporting UI (read-only DB reader) |

Both unit files are installed under `/etc/systemd/system/`. Both last (re)started **2026-07-07 06:29:48 UTC** — an unattended-upgrades (`needrestart`) auto-restart, tracked as **issue #20** and remediated; `NRestarts=0` since. `socket`-activation units are **not** present (zero-downtime restarts, #19, not yet deployed).

## Listening ports

| Proto | Port | Process | Purpose |
|---|---|---|---|
| UDP | 5060 | `sipgw` | SIP (inbound INVITE) |
| TCP | 5060 | `sipgw` | SIP (inbound INVITE) |
| TCP | 8080 | `sipgw-dashboard` | Web dashboard |

## Firewall & ingress filtering ⚠

- **No host firewall is active** — the `nftables` ruleset is effectively empty and `firewalld` is not installed.
- Ingress is filtered **at the application layer** by the SIP allowlist `sip.allowed_networks`: **`172.16.0.0/12`, `127.0.0.0/8`, `10.0.0.0/8`** — plus upstream network ACLs.
- **Recommendation (defense-in-depth):** add an `nftables` policy restricting **:5060** to the nurse-call/SIP sources and **:8080** to trusted management hosts. The dashboard has no authentication, so `:8080` exposure should be constrained at the network/firewall layer.

## Configuration (`/opt/sipgw/config.yaml` — secrets masked)

| Section | Key settings |
|---|---|
| `sip` | bind `0.0.0.0:5060`; allowlist `172.16.0.0/12, 127.0.0.0/8, 10.0.0.0/8`; `call_timeout_seconds: 1`; `immediate_bye: true`; RTP range 10000–20000 |
| `fusion` | base `https://api.icmobile.singlewire.com/api`; audience `2ffd6864-…`; scenario `4cba52d8-…` ("SIPtoTTSBridge"); endpoint `/v1/scenario-notifications`; field `customTTS` (`23435ce7-…`); `client_id`/`client_secret` **redacted** |
| `tts` | `play_count: 3`; `message_preamble: "Attention! Attention! "`; `iteration_preamble: ""` |
| `logging` | dir `/var/log/sipgw`; `retention_days: 90`; rotation midnight; `timezone: America/New_York` (declared; host clock is UTC — see note); api + sip debug logs on |
| `dashboard` | port 8080; bind `0.0.0.0`; `auto_refresh_seconds: 30`; `page_size: 20` |
| `database` | `/var/lib/sipgw/calls.db` |
| `dedupe` | **`enforce: true`**; `window_seconds: 2`; `match_bed: true`; `match_purpose: true` (clinically signed-off duplicate suppression) |

> **Timestamp note:** `logging.timezone` is set to `America/New_York` but the host clock is `Etc/UTC`, so emitted timestamps render in **UTC**. When reading logs, interpret times as UTC (subtract 4h for EDT / 5h for EST).

## Database

- SQLite `/var/lib/sipgw/calls.db` — **WAL** journal mode (`.db-wal` + `.db-shm` sidecars present); **307** call rows at capture.
- **Schema (`calls`)** — base columns plus the durable-delivery/outbox columns: `state`, `attempts`, `last_error`, `delivered_at`, `sip_call_id`, `duplicate_of`, `is_test`, **`event_id`**. Indexes: `idx_calls_created_at`, `idx_calls_state`, `idx_calls_event_id`.
- A pre-migration backup `calls.db.bak-int-columns` (2026-03-24) is retained alongside.

## Logs

Four daily-rotated streams in `/var/log/sipgw/` (compressed to `.tgz`, **90-day** retention, ~1.3 MB on disk at capture):

| Stream | Contents |
|---|---|
| `sipgw.log` | Application events (calls, delivery, lifecycle) |
| `sipgw_api_debug.log` | Northbound HTTP traces (OAuth + scenario POST) |
| `sipgw_sip_debug.log` | Raw inbound/outbound SIP messages |
| `sipgw_dashboard.log` | Dashboard service (new with the two-service split) |

Timestamps are **UTC**.

## Refreshing this section

This inventory is captured by the read-only host-inventory step of the `product-docs` skill (see the skill in `.claude/skills/product-docs/`). Re-run it on the host at each release to refresh the As-Built with the then-current build, schema, and configuration.
