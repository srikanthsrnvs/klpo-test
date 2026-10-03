#!/bin/sh
# Launch the KLPO+LoRA run on the box. Frees the GPUs from the standalone vLLM, makes sure the jev-browser tool
# server is up, then starts prime-rl's `rl` launcher detached. Log: /workspace/prl-runs/rl-launch.log
export PATH="$HOME/.local/bin:$PATH"
set -e
supervisorctl stop vllm >/dev/null 2>&1 || true
# Kill every leftover from earlier launches: uv wrappers, prime-rl python processes, vLLM router/engine, and
# anything still holding GPU memory. (Bracket patterns so pkill never matches this script's own command line.)
pkill -f "[r]l @ /workspace/jev-browser-rl" 2>/dev/null || true
pkill -f "[p]rime_rl" 2>/dev/null || true
pkill -f "[v]llm" 2>/dev/null || true
# Wait (up to 90 s) until every previous launch is really gone and both GPUs are empty before touching the run dir.
for i in $(seq 1 30); do
  for p in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null); do kill -9 "$p" 2>/dev/null || true; done
  pkill -9 -f "[r]l @ /workspace/jev-browser-rl" 2>/dev/null || true
  pkill -9 -f "[p]rime_rl" 2>/dev/null || true
  pkill -9 -f "[v]llm" 2>/dev/null || true
  pkill -9 -f "VLLM::" 2>/dev/null || true
  pkill -9 -f "PRL::" 2>/dev/null || true
  left=$(pgrep -fa "[p]rime_rl|[v]llm|[r]l @ /workspace" | wc -l)
  used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | awk "{s+=\$1} END {print s}")
  [ "$left" = "0" ] && [ "${used:-1}" -lt 1000 ] && break
  sleep 3
done
echo "leftover processes: $left, gpu MiB used: $used"
if [ "$CLEAN" = "1" ]; then rm -rf "/workspace/prl-runs/${RUN_NAME:-jev-klpo-r16}"; fi
# A shared tool server is only needed when the config points tasks at one (tools.url); otherwise every rollout
# launches its own server and we just verify that entry point imports and serves.
if grep -q "tools.url" "/workspace/jev-browser-rl/configs/${MODEL_CFG:-rl-vast.toml}"; then
  /workspace/prime-rl/.venv/bin/python /workspace/jev-browser-rl/scripts/checks/mcp_ready.py http://127.0.0.1:18100/mcp 20 || sh /workspace/jev-browser-rl/scripts/run/start_tools.sh
  /workspace/prime-rl/.venv/bin/python /workspace/jev-browser-rl/scripts/checks/mcp_ready.py http://127.0.0.1:18100/mcp 120 || { echo "tool server not ready; aborting launch"; exit 1; }
else
  N=1 sh /workspace/jev-browser-rl/scripts/checks/test_rollout_server.sh | grep -E "ROLLOUT SERVER TEST" || { echo "per-rollout tool server self-test failed; aborting launch"; exit 1; }
fi
nvidia-smi --query-gpu=index,memory.used --format=csv,noheader
mkdir -p /workspace/prl-runs
cd /workspace/prime-rl
RESUME_ARGS=""; [ -n "$RESUME_STEP" ] && RESUME_ARGS="--resume.step $RESUME_STEP"
setsid nohup uv run --no-sync rl @ /workspace/jev-browser-rl/configs/${MODEL_CFG:-rl-vast.toml} $RESUME_ARGS > /workspace/prl-runs/rl-launch.log 2>&1 < /dev/null &
echo "rl launched, pid $!"
