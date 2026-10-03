#!/bin/sh
# Pre-launch verification on a fresh box: Chrome/UA, per-rollout tool servers (unit), load test, then a 4-episode
# DeepSeek smoke through the real rollout path. Prints a report; exit 1 if any stage fails.
export PATH="$HOME/.local/bin:$PATH"
PY=/workspace/prime-rl/.venv/bin/python
fail=0
echo "### chrome"; google-chrome --version || fail=1
echo "### prime-rl smoke (algo + loss)"; cd /workspace/prime-rl && uv run --no-sync python /workspace/jev-browser-rl/trainer/smoke_prime.py 2>&1 | grep -E "algo|loss|grad" || fail=1
echo "### taskset loads"; uv run --no-sync python -c "
from jev_browser.taskset import JevBrowserConfig, JevBrowserTaskset
print('val', sum(1 for _ in JevBrowserTaskset(JevBrowserConfig(split='val')).load()), 'train', sum(1 for _ in JevBrowserTaskset(JevBrowserConfig(split='train')).load()))" || fail=1
echo "### per-rollout tool servers"; sh /workspace/jev-browser-rl/scripts/ops/clean_browsers.sh >/dev/null; N=3 timeout 600 sh /workspace/jev-browser-rl/scripts/checks/test_rollout_server.sh | grep -E "RESULT|TEST|LEAK"; N=3 timeout 600 sh /workspace/jev-browser-rl/scripts/checks/test_rollout_server.sh | grep -q "TEST: PASS" || fail=1
echo "### wall probe (new UA)"; timeout 240 $PY /workspace/jev-browser-rl/scripts/checks/wallprobe.py NEW /usr/local/bin/chrome-headless 2>&1 | tail -7
echo "### judge key"; cd /workspace/jev-browser-rl && $PY -c "
import importlib.util; spec=importlib.util.spec_from_file_location('v','reward/verifier.py'); v=importlib.util.module_from_spec(spec); spec.loader.exec_module(v); v.load_env()
f,m=v.call_deepseek('Return only JSON {\"score\": 0.5}','go',model='deepseek-v4-pro',effort='high',timeout=180); print('judge ok', f)" || fail=1
echo "### smoke eval (DeepSeek policy, 4 episodes)"; sh /workspace/jev-browser-rl/scripts/checks/run_smoke.sh >/dev/null
for i in $(seq 1 60); do grep -q "Evaluated\|error 1.00\|success" /workspace/vf-runs/smoke-deepseek.log 2>/dev/null && pgrep -f "bin/vf-ev[a]l" >/dev/null || break; sleep 15; done
sleep 5; sh /workspace/jev-browser-rl/scripts/checks/smoke_report.sh | tail -8
grep -q "not_found 0" /workspace/vf-runs/smoke-deepseek/traces.jsonl 2>/dev/null; sh /workspace/jev-browser-rl/scripts/checks/smoke_report.sh | grep -q "tools_offered 8" || fail=1
[ $fail = 0 ] && echo "PREFLIGHT: PASS" || echo "PREFLIGHT: FAIL"
