#!/bin/sh
# Start (or restart) the warm browser pool keeper: JEV_POOL_SIZE Chrome+daemon slots ready for per-rollout tool
# servers to adopt. The pool only has to cover rollout STARTS (the keeper refills 8 slots every ~3 s), not the inflight
# count; idle slots cost pids on a box whose cgroup counts threads. Log: /workspace/vf-runs/pool.log
export PATH="$HOME/.local/bin:$PATH"
pkill -9 -f "jev_browser[.]warm_pool" 2>/dev/null; sleep 1
mkdir -p /workspace/vf-runs
cd /workspace/prime-rl || exit 1
JEV_POOL_SIZE="${JEV_POOL_SIZE:-32}" setsid nohup uv run --no-sync python -m jev_browser.warm_pool > /workspace/vf-runs/pool.log 2>&1 < /dev/null &
for i in $(seq 1 "${WAIT:-90}"); do n=$(ls /tmp/jev-pool/*.ready 2>/dev/null | wc -l); [ "$n" -ge "${JEV_POOL_MIN:-8}" ] && break; sleep 1; done
echo "pool: $(ls /tmp/jev-pool/*.ready 2>/dev/null | wc -l) ready, $(ls /tmp/jev-pool/*.claimed-* 2>/dev/null | wc -l) claimed"
