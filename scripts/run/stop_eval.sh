#!/bin/sh
# Stop any standalone vf-eval runs (not the training launcher).
pkill -TERM -f "bin/vf-ev[a]l" 2>/dev/null; sleep 5; pkill -9 -f "bin/vf-ev[a]l" 2>/dev/null
echo "vf-eval processes left: $(pgrep -fc 'bin/vf-ev[a]l')"
