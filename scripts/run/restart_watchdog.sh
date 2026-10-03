#!/bin/sh
pkill -9 -f "watch_rl[.]sh" 2>/dev/null; sleep 1
cd /workspace && RUN_NAME="${RUN_NAME:-jev9b-klpo-r8}" MODEL_CFG="${MODEL_CFG:-rl-vast-9b.toml}" setsid nohup sh /workspace/jev-browser-rl/scripts/run/watch_rl.sh >> /workspace/prl-runs/watchdog.log 2>&1 < /dev/null &
sleep 1; echo "watchdog: $(pgrep -fc 'watch_rl[.]sh')"
