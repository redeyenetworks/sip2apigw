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

echo "########## M7 — immediate-BYE ACK-gated teardown + spec-correct BYE (#11) ##########"
# The 481 race is a real-socket, real-timing property a container cannot cover:
# INVITE -> 200 -> ACK -> the gateway BYE must be drawn only AFTER the ACK, target
# the caller's Contact, carry the reversed Record-Route, and log NO 481 / NO
# 'ACK for unknown call'. Case 2 withholds the ACK and asserts the lost-ACK
# fallback tears the dialog down within the window. Requires immediate_bye: true
# (set by deploy-staging.sh to mirror production).
systemctl restart "$W"; sleep 4
"$PY" - <<PY
import socket, sys, re
HOST, PORT = "127.0.0.1", $SIP_PORT
CONTACT = "sip:MedW_3404@127.0.0.1:40100"
RR = "<sip:127.0.0.1:$SIP_PORT;lr>"

def invite(call_id, lport):
    sdp = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\n"
           "t=0 0\r\nm=audio 40000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n")
    return (f"INVITE sip:gw@{HOST}:{PORT} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP 127.0.0.1:{lport};branch=z9hG4bK-m7-{call_id}\r\n"
            f"Record-Route: {RR}\r\n"
            f'From: "Code Blue" <sip:MedW_3404@127.0.0.1>;tag=m7\r\n'
            f"To: <sip:gw@{HOST}:{PORT}>\r\n"
            f"Call-ID: {call_id}\r\nCSeq: 1 INVITE\r\n"
            f"Contact: <{CONTACT}>\r\n"
            f"Content-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}").encode()

def ack(call_id, lport, to_tag):
    return (f"ACK {CONTACT} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP 127.0.0.1:{lport};branch=z9hG4bK-m7ack-{call_id}\r\n"
            f'From: "Code Blue" <sip:MedW_3404@127.0.0.1>;tag=m7\r\n'
            f"To: <sip:gw@{HOST}:{PORT}>;tag={to_tag}\r\nCall-ID: {call_id}\r\n"
            f"CSeq: 1 ACK\r\nContent-Length: 0\r\n\r\n").encode()

def recv_until(s, want_method=None, want_status=None, deadline=3.0):
    s.settimeout(deadline)
    try:
        while True:
            data, _ = s.recvfrom(65535)
            first = data.split(b"\r\n", 1)[0].decode("latin-1")
            if want_status and first.startswith("SIP/2.0") and str(want_status) in first:
                return data, first
            if want_method and first.startswith(want_method + " "):
                return data, first
    except socket.timeout:
        return None, None

def to_tag_of(resp):
    for line in resp.decode("latin-1").split("\r\n"):
        if line[:3].lower() in ("to:", "t: ") or line.lower().startswith("to:"):
            m = re.search(r"tag=([^;\s]+)", line)
            if m: return m.group(1)
    return "x"

ok = True

# --- Case 1: INVITE -> 200 -> ACK -> gateway BYE (Contact + Route) ---
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 40100))
s.sendto(invite("m7-case1@127.0.0.1", 40100), (HOST, PORT))
resp, _ = recv_until(s, want_status=200)
if not resp:
    print("    FAIL: no 200 OK for INVITE"); ok = False
else:
    s.sendto(ack("m7-case1@127.0.0.1", 40100, to_tag_of(resp)), (HOST, PORT))
    bye, line = recv_until(s, want_method="BYE")
    if not bye:
        print("    FAIL: no gateway BYE after ACK"); ok = False
    else:
        uri = line.split()[1]
        has_route = any(l.lower().startswith("route:") for l in bye.decode("latin-1").split("\r\n"))
        print(f"    BYE request-line: {line}")
        if uri != CONTACT: print(f"    FAIL: BYE request-URI {uri} != Contact {CONTACT}"); ok = False
        if not has_route: print("    FAIL: BYE carried no Route header"); ok = False
        if uri == CONTACT and has_route: print("    case1: BYE targets Contact + carries reversed Record-Route")
s.close()

# --- Case 2: INVITE -> 200 -> (withhold ACK) -> lost-ACK fallback BYE ---
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 40102))
s.sendto(invite("m7-case2@127.0.0.1", 40102), (HOST, PORT))
resp, _ = recv_until(s, want_status=200)
if not resp:
    print("    FAIL: no 200 OK for INVITE (case2)"); ok = False
else:
    bye, line = recv_until(s, want_method="BYE", deadline=5.0)  # > ack fallback window
    if bye: print("    case2: lost-ACK fallback drew the BYE within the window")
    else:   print("    FAIL: lost-ACK fallback did NOT tear the dialog down"); ok = False
s.close()

sys.exit(0 if ok else 1)
PY
M7_RC=$?
sleep 1
BAD481=$(grep -a 'm7-case' "$LOG" | grep -c ' 481 ')
BADACK=$(grep -ac 'ACK for unknown call m7-case' "$LOG")
echo "  481 lines: $BAD481 | 'ACK for unknown call' lines: $BADACK"
[[ "$M7_RC" == "0" && "$BAD481" == "0" && "$BADACK" == "0" ]] \
  && pass "immediate-BYE is ACK-gated + spec-correct; zero 481 / zero unknown-ACK" \
  || fail "immediate-BYE teardown drill — investigate before cutover (rc=$M7_RC 481=$BAD481 unkAck=$BADACK)"

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
