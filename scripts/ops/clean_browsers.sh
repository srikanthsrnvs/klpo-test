#!/bin/sh
# Kill every stray tool server, browser-harness daemon and headless Chrome (not the training run).
# Bracketed patterns so the regex never matches a command line that merely mentions these names.
pkill -9 -f "serve_tools[.]py" 2>/dev/null
pkill -9 -f "jev_browser[.]warm_pool" 2>/dev/null; rm -rf /tmp/jev-pool
pkill -9 -f "jev_browser[.]toolset" 2>/dev/null
pkill -9 -f "browser_harness[.]daemon" 2>/dev/null
pkill -9 -f "/opt/google/chrome/[c]hrome" 2>/dev/null
pkill -9 -f "[g]oogle-chrome" 2>/dev/null
sleep 2
sh /workspace/jev-browser-rl/scripts/ops/status_procs.sh
