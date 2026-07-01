# sipgw v1.6.0 — cutover-host staging drills

Runs the real-systemd checks that a container cannot do, on the cutover host, in a
**separately-named, dry-run, alt-port** deployment. It never touches the live
`sipgw.service`, ports 5060/8080, `/opt/sipgw`, or the prod DB `/var/lib/sipgw`.

## Run (on the cutover host, as a sudo-capable user)

```bash
sudo git clone -b release/v1.6.0 https://github.com/redeyenetworks/sip2apigw.git /opt/sipgw-staging
sudo bash /opt/sipgw-staging/deploy/host-staging/deploy-staging.sh
sudo bash /opt/sipgw-staging/deploy/host-staging/drills.sh          # ~4–5 min (M2 waits on the 30s watchdog)
# review PASS/FAIL, then:
sudo bash /opt/sipgw-staging/deploy/host-staging/teardown-staging.sh
```

Footprint: unit `sipgw-staging` (SIP 127.0.0.1:5062, `Type=notify`, `WatchdogSec=30`),
unit `sipgw-dashboard-staging` (127.0.0.1:8082, read-only, `MemoryMax=256M`,
`CPUQuota=50%`), DB `/var/lib/sipgw-staging`, logs `/var/log/sipgw-staging`.
Both units carry `dry_run: true` **and** `SIPGW_DRY_RUN=1`, so no real page can send.

## Drills → gates they close

| Drill | Proves | Gate |
|---|---|---|
| **M1** | `Type=notify` writer reaches `active` (READY=1 arrives) | #8 |
| **M5** | two-process boot: dashboard `/health` 200, page delivered, **zero unmarked / no real send** | #14 + safety |
| **M4** | read-only reader builds `-shm/-wal` and serves under `ProtectSystem=strict` | #14 |
| **M3** | dashboard crash leaves the **writer/pager untouched**; `MemoryMax`/`CPUQuota` applied + kernel-enforced | #14 |
| **M6** | page orphaned mid-delivery is recovered → delivered after `systemctl restart` | #2 |
| **M2** | frozen event loop (SIGSTOP) → watchdog **restarts within ~30s**, no 3-min restart loop | #8 |

All PASS ⇒ the real-systemd deferrals are cleared. Cutover then needs only the
operational preconditions (ward on fallback paging, live tester, prod creds/
`scenario_id`, prod DB `is_test=0`-clean) and your explicit "execute" — see
`docs/RUNBOOK-cutover-2026-07-01.md`.
