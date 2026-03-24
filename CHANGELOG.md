# Changelog

All notable changes to the sipgw project are documented in this file.

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
