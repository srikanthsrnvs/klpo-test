#!/bin/sh
# Unit test of the per-rollout tool server: start N servers the way verifiers does (python -m jev_browser.toolset
# with VF_CONFIG/MCP_PORT_FILE), load-test each with 4 clients, SIGTERM them, and verify their Chrome + daemon died.
export PATH="$HOME/.local/bin:$PATH"
N="${N:-3}"; PY=/workspace/prime-rl/.venv/bin/python; cd /workspace/prime-rl || exit 1
before_chrome=$(pgrep -fc "google-chrome"); before_daemon=$(pgrep -fc "browser_harness.daemon")
pids=""; ports=""
for i in $(seq 1 $N); do
  mkdir -p /root/.cache/verifiers/runtimes/subprocess/unit-test-$i-with-a-deliberately-long-directory-name-like-verifiers-uses
  PF=/tmp/vf-port-test-$i; rm -f $PF
  VF_CONFIG='{"colocated": true}' MCP_PORT_FILE=$PF MCP_HOST=127.0.0.1 TMPDIR=/root/.cache/verifiers/runtimes/subprocess/unit-test-$i-with-a-deliberately-long-directory-name-like-verifiers-uses setsid $PY -m jev_browser.toolset > /tmp/rollout-server-$i.log 2>&1 < /dev/null &
  pids="$pids $!"
done
for i in $(seq 1 $N); do
  for t in $(seq 1 60); do [ -s /tmp/vf-port-test-$i ] && break; sleep 1; done
  ports="$ports $(cat /tmp/vf-port-test-$i 2>/dev/null)"
done
echo "server pids:$pids ports:$ports"
echo "chrome procs: $before_chrome -> $(pgrep -fc google-chrome)   daemons: $before_daemon -> $(pgrep -fc browser_harness.daemon)"
fail=0
for p in $ports; do
  [ -z "$p" ] && { echo "a server never published its port"; fail=1; continue; }
  $PY /workspace/jev-browser-rl/scripts/checks/loadtest_tools.py --url http://127.0.0.1:$p/mcp --clients 4 2>&1 | grep -E "clients=|RESULT|FAIL" | head -3 || fail=1
done
for p in $pids; do kill -TERM $p 2>/dev/null; done; sleep 6
echo "after stop: chrome procs: $(pgrep -fc google-chrome)  daemons: $(pgrep -fc browser_harness.daemon)  servers alive: $(for p in $pids; do kill -0 $p 2>/dev/null && echo -n x; done)"
[ "$(pgrep -fc google-chrome)" -le "$before_chrome" ] || { echo "LEAK: chrome processes left behind"; fail=1; }
[ "$(pgrep -fc browser_harness.daemon)" -le "$before_daemon" ] || { echo "LEAK: daemons left behind"; fail=1; }
grep -l "Traceback" /tmp/rollout-server-*.log 2>/dev/null && { echo "server tracebacks:"; grep -h -A3 Traceback /tmp/rollout-server-*.log | tail -8; fail=1; }
[ $fail = 0 ] && echo "ROLLOUT SERVER TEST: PASS" || echo "ROLLOUT SERVER TEST: FAIL"
