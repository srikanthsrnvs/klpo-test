#!/bin/sh
# Full clean relaunch: stop watchdog + orphans, start the run, start the watchdog. Patterns live here so an ssh
# command line that invokes this script never matches its own pkill.
export PATH="$HOME/.local/bin:$PATH"
RUN_NAME="${RUN_NAME:-jev9b-klpo-r8}"; MODEL_CFG="${MODEL_CFG:-rl-vast-9b.toml}"
pkill -9 -f "watch_rl[.]sh" 2>/dev/null
sh /workspace/jev-browser-rl/scripts/ops/kill_prl.sh
sleep 2
# The shared tool server (and its Chrome on port 9333) only exists for configs that point tasks at tools.url.
if grep -q "tools.url" "/workspace/jev-browser-rl/configs/$MODEL_CFG"; then
  pkill -9 -f "remote-debugging-port=9333" 2>/dev/null
  WAIT=35 sh /workspace/jev-browser-rl/scripts/run/start_tools.sh | head -1
fi
# Per-rollout configs: keep a warm pool of Chrome+daemon slots so tool servers start in milliseconds.
if ! grep -q "tools.url" "/workspace/jev-browser-rl/configs/$MODEL_CFG"; then
  JEV_POOL_SIZE="${JEV_POOL_SIZE:-104}" sh /workspace/jev-browser-rl/scripts/run/start_pool.sh
fi
CLEAN="${CLEAN:-0}" RUN_NAME="$RUN_NAME" MODEL_CFG="$MODEL_CFG" WAIT=30 sh /workspace/jev-browser-rl/scripts/run/start_rl.sh
cd /workspace && RUN_NAME="$RUN_NAME" MODEL_CFG="$MODEL_CFG" setsid nohup sh /workspace/jev-browser-rl/scripts/run/watch_rl.sh > /workspace/prl-runs/watchdog.log 2>&1 < /dev/null &
sh /workspace/jev-browser-rl/scripts/run/start_dashboard.sh | head -1
sleep 2
echo "watchdog: $(pgrep -fc 'watch_rl[.]sh')  rl: $(pgrep -fc '[r]l @ /workspace')"
