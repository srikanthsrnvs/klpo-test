#!/bin/sh
# GPU-free end-to-end smoke test: 4 validation episodes through the real rollout path with DeepSeek as the policy.
export PATH="$HOME/.local/bin:$PATH"
# The judge key comes from the same loader the verifier uses (repo .env, then ~/.jev-secrets.env).
export DEEPSEEK_API_KEY=$(cd /workspace/jev-browser-rl && /workspace/prime-rl/.venv/bin/python -c "import importlib.util,os; s=importlib.util.spec_from_file_location('v','reward/verifier.py'); v=importlib.util.module_from_spec(s); s.loader.exec_module(v); v.load_env(); print(os.environ.get('DEEPSEEK_API_KEY',''))")
sh /workspace/jev-browser-rl/scripts/ops/clean_browsers.sh
rm -rf /workspace/vf-runs/smoke-deepseek /workspace/vf-runs/tool-servers
cd /workspace/prime-rl && setsid nohup uv run --no-sync vf-eval @ /workspace/jev-browser-rl/configs/eval-smoke.toml --run.dir smoke-deepseek > /workspace/vf-runs/smoke-deepseek.log 2>&1 < /dev/null &
echo "smoke launched"
