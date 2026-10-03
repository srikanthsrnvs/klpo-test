#!/bin/sh
# Stop the running rl launcher (and its children); the watchdog relaunches with --resume from the last checkpoint.
pkill -TERM -f "[r]l @ /workspace/jev-browser-rl" 2>/dev/null
sleep 15
pkill -9 -f "[p]rime_rl" 2>/dev/null; pkill -9 -f "VLLM::" 2>/dev/null; pkill -9 -f "PRL::" 2>/dev/null
echo "stopped; watchdog will resume: $(ls -d /workspace/prl-runs/${RUN_NAME:-jev9b-klpo-r8}/checkpoints/step_* 2>/dev/null | tail -1)"
