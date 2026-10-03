#!/bin/sh
# Watchdog: if the rl launcher dies, relaunch with --resume (from the latest checkpoint) after cleaning leftovers.
# Runs detached: setsid nohup sh watch_rl.sh > /workspace/prl-runs/watchdog.log 2>&1 < /dev/null &
export PATH="$HOME/.local/bin:$PATH"
RUN_NAME="${RUN_NAME:-jev9b-klpo-r8}"; MODEL_CFG="${MODEL_CFG:-rl-vast-9b.toml}"
while true; do
  sleep 120
  if ! pgrep -f "[r]l @ /workspace/jev-browser-rl" >/dev/null; then
    echo "$(date) rl not running; relaunching with --resume" 
    pkill -9 -f "[p]rime_rl" 2>/dev/null; pkill -9 -f "[v]llm" 2>/dev/null; pkill -9 -f "VLLM::" 2>/dev/null; pkill -9 -f "PRL::" 2>/dev/null
    for p in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null); do kill -9 "$p" 2>/dev/null; done
    sleep 10
    cd /workspace/prime-rl || exit 1
    # resume from the newest checkpoint that has BOTH trainer and orchestrator parts (a kill mid-step leaves a partial one)
    LAST=""
    for d in $(ls -d "/workspace/prl-runs/$RUN_NAME/checkpoints/step_"* 2>/dev/null | sed -E "s/.*step_//" | sort -n); do
      if [ -d "/workspace/prl-runs/$RUN_NAME/checkpoints/step_$d/trainer" ] && [ -d "/workspace/prl-runs/$RUN_NAME/checkpoints/step_$d/orchestrator" ]; then LAST="$d"; fi
    done
    if [ -n "$LAST" ]; then
      for d in $(ls -d "/workspace/prl-runs/$RUN_NAME/checkpoints/step_"* 2>/dev/null | sed -E "s/.*step_//"); do [ "$d" -gt "$LAST" ] && rm -rf "/workspace/prl-runs/$RUN_NAME/checkpoints/step_$d"; done
      RESUME="--resume.step $LAST"
    else RESUME=""; mv "/workspace/prl-runs/$RUN_NAME" "/workspace/prl-runs/$RUN_NAME.dead.$(date +%H%M)" 2>/dev/null; fi
    setsid nohup uv run --no-sync rl @ "/workspace/jev-browser-rl/configs/$MODEL_CFG" $RESUME >> /workspace/prl-runs/rl-launch.log 2>&1 < /dev/null &
    echo "$(date) relaunched pid $! ($RESUME)"
    sh /workspace/jev-browser-rl/scripts/run/start_dashboard.sh >/dev/null 2>&1
    sleep 600
  fi
done
