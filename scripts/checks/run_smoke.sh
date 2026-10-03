#!/bin/sh
# GPU-free end-to-end smoke test: 4 validation episodes through the real rollout path with DeepSeek as the policy.
export PATH="$HOME/.local/bin:$PATH"
export DEEPSEEK_API_KEY=$(grep "^DEEPSEEK_API_KEY=" /root/.jev-secrets.env | cut -d= -f2)
sh /workspace/jev-browser-rl/scripts/ops/clean_browsers.sh
rm -rf /workspace/vf-runs/smoke-deepseek /workspace/vf-runs/tool-servers
cd /workspace/prime-rl && setsid nohup uv run --no-sync vf-eval @ /workspace/jev-browser-rl/configs/eval-smoke.toml --run.dir smoke-deepseek > /workspace/vf-runs/smoke-deepseek.log 2>&1 < /dev/null &
echo "smoke launched"
