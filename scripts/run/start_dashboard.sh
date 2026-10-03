#!/bin/sh
# prime-rl web dashboard for all runs under /workspace/prl-runs, on 127.0.0.1:${DASH_PORT:-8080} (reach it through an ssh tunnel:
#   ssh -p 41193 root@216.129.245.167 -L 7788:localhost:7788   then open http://localhost:7788 )
export PATH="$HOME/.local/bin:$PATH"
pkill -f "prime_rl.entrypoints.dashboard|[d]ashboard --host" 2>/dev/null; sleep 1
cd /workspace/prime-rl && setsid nohup uv run --no-sync dashboard --isolated --host 127.0.0.1 --port ${DASH_PORT:-8080} /workspace/prl-runs > /workspace/prl-runs/dashboard.log 2>&1 < /dev/null &
sleep 12
curl -s -m 5 -o /dev/null -w "dashboard http %{http_code}\n" http://127.0.0.1:${DASH_PORT:-8080}/
tail -3 /workspace/prl-runs/dashboard.log | cut -c1-160
