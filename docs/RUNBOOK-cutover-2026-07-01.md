# sipgw v1.6.0 — Cutover Runbook

**Status:** IN PREPARATION. Cutover is human- and clinical-gated. Do NOT execute
until the §6 pre-flight in `fixesprompt.md` is fully satisfied and a new window
is scheduled. Production currently runs v1.5.1 (healthy).

This document is authored during the build so the deploy/rollback mechanics —
especially the **no-data-loss guarantee for per-install config** — are settled
in advance. Backup/pin IDs, the outage-timer value, and the TEST-scenario status
are filled in at cutover time.

---

## 0. Per-install config preservation (lookups.yaml + config.yaml) — HARD GATE

`config.yaml` and (as of v1.6.0) `lookups.yaml` are **per-install, authoritative
on each box**, and are gitignored. `config.yaml` was already untracked; v1.6.0
**untracks `lookups.yaml`** so future deploys can never touch a site's custom
area/room map. The one-time transitional deploy still flips `lookups.yaml` from
tracked→untracked, so it MUST be preserved explicitly and proven byte-identical.

**These files must be byte-identical before and after the deploy, or ABORT:**

```bash
STAMP=$(TZ=America/New_York date +%Y%m%d-%H%M%S)EDT
SAFE=/var/backups/sipgw/perinstall-$STAMP
sudo mkdir -p "$SAFE"

# 1. Snapshot the live per-install files BEFORE touching git.
sudo cp -a /opt/sipgw/lookups.yaml "$SAFE/lookups.yaml"
sudo cp -a /opt/sipgw/config.yaml  "$SAFE/config.yaml"
sudo sha256sum /opt/sipgw/lookups.yaml /opt/sipgw/config.yaml | sudo tee "$SAFE/pre.sha256"

# 2. Deploy the release. -f is required because lookups.yaml has local edits and
#    is tracked at v1.5.1; the checkout to v1.6.0 (where it is untracked) would
#    otherwise refuse or, with -f, remove it — hence the restore in step 3.
sudo git -C /opt/sipgw fetch --tags origin
sudo git -C /opt/sipgw checkout -f v1.6.0

# 3. Restore the authoritative per-install files.
sudo cp -a "$SAFE/lookups.yaml" /opt/sipgw/lookups.yaml
sudo cp -a "$SAFE/config.yaml"  /opt/sipgw/config.yaml
sudo chown sipgw:sipgw /opt/sipgw/lookups.yaml /opt/sipgw/config.yaml

# 4. HARD GATE: prove byte-identical. Any diff => STOP and roll back.
sudo diff /opt/sipgw/lookups.yaml "$SAFE/lookups.yaml" \
  && sudo diff /opt/sipgw/config.yaml "$SAFE/config.yaml" \
  && echo "PER-INSTALL PRESERVED (byte-identical)" \
  || { echo "PER-INSTALL MISMATCH — ABORT/ROLLBACK"; exit 1; }
```

> After v1.6.0, `lookups.yaml` is untracked on the box, so every *subsequent*
> deploy leaves it alone automatically. This preservation dance is only needed
> for the v1.5.1→v1.6.0 transition.

**Staging rehearsal (required before cutover):** run steps 1–4 against a scratch
copy of `/opt/sipgw` in staging and confirm the byte-diff gate passes. Tracked in
the §5 drill matrix (fallback/deploy proof).

---

## 1. What ships / what defers

Filled from the release as it lands. Built and drilled so far:
- **§2 safety substrate** — no-send transport gate, `[TEST]` marker, prod-DB
  barrier, mock server.
- **#2 durable delivery** — WAL + idempotent migration (drilled lossless on a
  301-row prod-DB copy), record-first outbox, retry worker (backoff,
  Retry-After, recovery, expiry), integrated + boot-smoke green (zero real send).

Pending (see `fixesprompt.md` §4 order): #11, #9, #4, #6, #10, #3; then the tail
(#12, #8, #7, #14, #15, #5-shadow, #13-P1); then the full §5 drill matrix.

**Deferred by policy (not clock):** #5 enforcement stays OFF (dedupe ships
shadow/disabled until clinical signs off on a window); #13 Phases 2–4 ship
post-cutover on the decoupled dashboard.

---

## 2. Pre-flight (ALL true or do not start)

Per `fixesprompt.md` §6. Highlights:
- Tier-A + §5 minimum drills green on `release/v1.6.0`.
- Migration validated on a prod-DB copy; rollback rehearsed on staging < 5 min;
  the OLD artifact booted and proven to run; **per-install preservation rehearsed
  (§0)**.
- `fusion.dry_run` OFF in prod; `systemctl show sipgw -p Environment` has no
  `SIPGW_DRY_RUN`; the deploy shell never exported it.
- Prod DB has zero `is_test=1` rows.
- **TEST-scenario status:** _no dedicated Fusion TEST scenario_ (per operator
  decision to use the mock for all testing). Record here whether the human
  accepts cutover verification via logs+dashboard only, or defers. NEVER fire the
  production Code Blue scenario to verify.
- Fresh §3 backup + GH pin `v1.6.0-precutover-<stamp>` (pristine, PRE-migration).
- Ward notified: brief window + fallback paging procedure in effect (Rauland
  gets 200 OK and believes pages succeed even while sipgw is stopped).

Backup IDs / pin / outage-timer value: _fill at cutover_.

---

## 3. Cutover / verification / rollback

- Cutover steps, outage timer, and verification: `fixesprompt.md` §6.
- Rollback command block (must complete < 5 min): `fixesprompt.md` §6 Rollback,
  **plus** restore per-install files from `$SAFE` (§0) after the app tar is
  restored.
- Verification uses the mock/logs/dashboard (no TEST scenario). The first real
  page after cutover is the true end-to-end confirmation — a residual the human
  accepts at flip time.
