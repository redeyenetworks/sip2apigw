#!/usr/bin/env bash
# Real-systemd drill set for sipgw v1.6.0 — the checks that a container cannot do.
# STAGING ONLY (sipgw-staging / sipgw-dashboard-staging on :5062/:8082, dry-run).
# Prints PASS/FAIL per drill. Run: sudo bash drills.sh
set -uo pipefail

STAGING_DIR="/opt/sipgw-staging"
DB="/var/lib/sipgw-staging/calls.db"
LOG="/var/log/sipgw-staging/sipgw.log"
PY="$STAGING_DIR/venv/bin/python"
SIP_PORT=5062; DASH_PORT=8082
W=sipgw-staging.service; D=sipgw-dashboard-staging.service
pass(){ echo "  PASS: $1"; }
fail(){ echo "  FAIL: $1"; }

echo "########## M1 — Type=notify READY (#8) ##########"
systemctl restart "$W"; sleep 4
[[ "$(systemctl is-active $W)" == "active" ]] && pass "writer reached active (READY=1 received)" || fail "writer not active — READY not sent?"
systemctl show -p Type,WatchdogUSec,NRestarts "$W" | sed 's/^/    /'

echo "########## M5 — two-process boot smoke + NO real send ##########"
systemctl restart "$D"; sleep 2
echo "  /health: $(curl -s --max-time 3 http://127.0.0.1:$DASH_PORT/health)"
"$PY" - <<PY
import socket
sdp="v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 40000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
m=("INVITE sip:gw@127.0.0.1:$SIP_PORT SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:40100;branch=z9hG4bK-drill\r\n"
   'From: "Code Blue" <sip:a730r201@127.0.0.1>;tag=drill\r\nTo: <sip:gw@127.0.0.1:$SIP_PORT>\r\n'
   "Call-ID: drill@127.0.0.1\r\nCSeq: 1 INVITE\r\nContact: <sip:a730r201@127.0.0.1:40100>\r\n"
   f"Content-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}")
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(3);s.sendto(m.encode(),("127.0.0.1",$SIP_PORT));s.close()
PY
sleep 2
UNMARKED=$(grep -a 'sipgw' "$LOG" | grep -vc '\[TEST\]')
BLOCKED=$(grep -ac 'DRY-RUN blocked' "$LOG")
STATE=$("$PY" -c "import sqlite3;c=sqlite3.connect('file:$DB?mode=ro',uri=True);print(c.execute('SELECT state,is_test FROM calls ORDER BY id DESC LIMIT 1').fetchone())")
echo "  last row: $STATE | blocked-send lines: $BLOCKED | unmarked lines: $UNMARKED"
[[ "$UNMARKED" == "0" ]] && pass "zero unmarked / no real send" || fail "UNMARKED LINES — investigate before cutover"

echo "########## M4 — WAL -shm/-wal under ProtectSystem=strict (#14) ##########"
ls -la /var/lib/sipgw-staging/ | sed 's/^/    /'
SHM=$(ls /var/lib/sipgw-staging/calls.db-shm 2>/dev/null && echo yes)
HB=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:$DASH_PORT/)
[[ -n "$SHM" && "$HB" == "200" ]] && pass "reader built -shm and served page under ProtectSystem=strict" || fail "reader could not build -shm / serve (check ReadWritePaths)"

echo "########## M3 — resource caps + crash isolation (#14) ##########"
systemctl show -p MemoryMax,CPUQuotaPerSecUSec "$D" | sed 's/^/    /'
WP=$(systemctl show -p MainPID --value "$W")
systemctl kill -s KILL "$D"; sleep 4
[[ "$(systemctl is-active $W)" == "active" && "$WP" == "$(systemctl show -p MainPID --value $W)" ]] \
  && pass "writer (pager) UNAFFECTED by dashboard crash" || fail "dashboard crash disturbed the writer"
[[ "$(systemctl is-active $D)" == "active" ]] && pass "dashboard auto-restarted" || fail "dashboard did not restart"
echo "  -- optional kernel MemoryMax enforcement proof (separate scope):"
systemd-run --scope -p MemoryMax=64M --quiet "$PY" -c "b=bytearray(256*1024*1024); print('    NOT killed (MemoryMax NOT enforced?)')" \
  2>/dev/null || echo "    OOM-killed at 64M -> systemd MemoryMax is enforced on this host"

echo "########## M6 — restart-recovery under systemd (#2) ##########"
sudo -u sipgw "$PY" - <<PY
import asyncio
from sipgw.database import CallDatabase
async def main():
    db=CallDatabase("$DB"); await db.initialize()
    cid=await db.create_pending_call(caller_id="a730r201",display_name="Code Blue",
        area_number="730",area_name="1st Floor... E.D...",room_number="201",
        tts_string="Attention! Code Blue! ...",is_test=1)
    await db.mark_attempting(cid)   # orphan it in 'delivering'
    print("    seeded orphan call",cid); await db.close()
asyncio.run(main())
PY
systemctl restart "$W"; sleep 4
grep -a 'Recovered' "$LOG" | tail -1 | sed 's/^/    /'
RSTATE=$("$PY" -c "import sqlite3;c=sqlite3.connect('file:$DB?mode=ro',uri=True);print(c.execute(\"SELECT state FROM calls WHERE caller_id='a730r201' ORDER BY id DESC LIMIT 1\").fetchone()[0])")
[[ "$RSTATE" == "delivered" ]] && pass "orphaned page recovered -> delivered" || fail "recovery did not deliver (state=$RSTATE)"

echo "########## M2 — watchdog restart on a hung loop (#8) — takes ~4 min ##########"
MP=$(systemctl show -p MainPID --value "$W")
echo "  freezing writer PID $MP (SIGSTOP) — watchdog should kill+restart within ~WatchdogSec(30s)..."
kill -STOP "$MP"
sleep 45
NP=$(systemctl show -p MainPID --value "$W")
[[ "$(systemctl is-active $W)" == "active" && "$MP" != "$NP" ]] && pass "watchdog fired -> restarted (PID $MP -> $NP)" || fail "no watchdog restart"
journalctl -u "$W" --since "2 min ago" --no-pager | grep -i 'watchdog\|killing\|timeout' | tail -3 | sed 's/^/    /'
echo "  no-restart-loop check (3 x 60s):"
for i in 1 2 3; do sleep 60; echo "    min $i: active=$(systemctl is-active $W) NRestarts=$(systemctl show -p NRestarts --value $W)"; done
echo "  (expect NRestarts stable after the single SIGSTOP restart; service stays active)"

echo ""
echo "########## DONE. Review PASS/FAIL above. Teardown when finished: ##########"
echo "  sudo bash $STAGING_DIR/deploy/host-staging/teardown-staging.sh"
