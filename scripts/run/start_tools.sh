#!/bin/sh
# Start (or restart) the standalone jev-browser tool server on the box. Logs: /workspace/vf-runs/tools.log
export PATH="$HOME/.local/bin:$PATH"
pkill -9 -f "serve_tools[.]py" 2>/dev/null; sleep 2; pkill -9 -f "serve_tools[.]py" 2>/dev/null
# the server's Chrome lives in its own session; kill every headless Chrome tree so none is orphaned
pkill -9 -f "google-chrome .*--remote-debugging-port" 2>/dev/null; pkill -9 -f "/opt/google/chrome/chrome" 2>/dev/null; sleep 2
sleep 1
mkdir -p /workspace/vf-runs
cd /workspace/prime-rl || exit 1
JEV_MAX_SESSIONS="${JEV_MAX_SESSIONS:-64}" JEV_SESSION_IDLE_S="${JEV_SESSION_IDLE_S:-1200}" JEV_DISABLE_SCREENSHOT="${JEV_DISABLE_SCREENSHOT:-1}" MCP_PORT="${MCP_PORT:-18100}" setsid nohup uv run --no-sync python /workspace/jev-browser-rl/scripts/run/serve_tools.py > /workspace/vf-runs/tools.log 2>&1 < /dev/null &
# Readiness = a real MCP initialize + tools/list (Chrome is launched before the port binds, so this also waits for Chrome).
/workspace/prime-rl/.venv/bin/python /workspace/jev-browser-rl/scripts/checks/mcp_ready.py "http://127.0.0.1:${MCP_PORT:-18100}/mcp" "${WAIT:-120}"
tail -5 /workspace/vf-runs/tools.log | cut -c1-200
