#!/bin/sh
# Stop watchdog + run (keeps checkpoints, tool server and tracer running).
pkill -9 -f "watch_rl[.]sh" 2>/dev/null
pkill -TERM -f "[r]l @ /workspace/jev-browser-rl" 2>/dev/null; sleep 10
pkill -9 -f "[p]rime_rl" 2>/dev/null; pkill -9 -f "VLLM::" 2>/dev/null; pkill -9 -f "PRL::" 2>/dev/null
echo "stopped: watchdog=$(pgrep -fc 'watch_rl[.]sh') rl=$(pgrep -fc '[r]l @ /workspace')"
