#!/bin/bash
# One-shot bootstrap of a fresh Vast box for the jev KLPO runs. Idempotent-ish. Log: /workspace/bootstrap.log
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
echo "== $(date) stop template vllm"; supervisorctl stop vllm >/dev/null 2>&1 || true
echo "== chrome"; if ! command -v google-chrome >/dev/null; then
  apt-get update -qq >/dev/null; apt-get install -y -qq wget gnupg2 fonts-liberation libasound2t64 libatk-bridge2.0-0 libatk1.0-0 libcups2 libgbm1 libgtk-3-0 libnss3 libxss1 libxcomposite1 libxdamage1 libxrandr2 xdg-utils bc >/dev/null 2>&1 || apt-get install -y -qq wget gnupg2 fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 libcups2 libgbm1 libgtk-3-0 libnss3 libxss1 libxcomposite1 libxdamage1 libxrandr2 xdg-utils bc >/dev/null 2>&1
  wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb && (apt-get install -y -qq /tmp/chrome.deb >/dev/null 2>&1 || dpkg -i /tmp/chrome.deb >/dev/null 2>&1 || apt-get -f install -y -qq >/dev/null 2>&1)
fi; google-chrome --version
cat > /usr/local/bin/chrome-headless <<'W'
#!/usr/bin/env bash
exec /usr/bin/google-chrome --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage --window-size=1280,900 \
  --renderer-process-limit=8 --disable-site-isolation-trials --disable-features=IsolateOrigins,site-per-process \
  --disable-extensions --disable-background-networking --no-first-run \
  --disable-blink-features=AutomationControlled --lang=en-US --accept-lang=en-US,en \
  --user-agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36" "$@"
W
chmod +x /usr/local/bin/chrome-headless
grep -q JEV_CHROME_BINARY /etc/environment || echo 'JEV_CHROME_BINARY=/usr/local/bin/chrome-headless' >> /etc/environment
echo "== prime-rl"; cd /workspace
git config --global url."https://github.com/".insteadOf "git@github.com:"
[ -d prime-rl ] || git clone -q https://github.com/PrimeIntellect-ai/prime-rl
cd prime-rl && git submodule update --init --recursive -q 2>&1 | tail -1 || true
uv sync --all-extras > /workspace/prime-rl-install.log 2>&1 && echo "uv sync ok" || { tail -5 /workspace/prime-rl-install.log; exit 1; }
echo "== patch + packages"; python3 /workspace/jev-browser-rl/trainer/patch_prime_rl.py /workspace/prime-rl
cp /workspace/jev-browser-rl/trainer/klpo_prime.py /workspace/prime-rl/src/klpo_prime.py
uv pip install -q -e /workspace/jev-browser-rl/harness -e /workspace/jev-browser-rl/env "cryptography>=42" websockets
uv run --no-sync python /workspace/jev-browser-rl/trainer/smoke_prime.py 2>&1 | tail -2
echo "== prime cli"; uv tool install -q prime 2>/dev/null || true
echo "== model"; mkdir -p /workspace/models; [ -f /workspace/models/Qwen3.5-9B/config.json ] || uv run --no-sync hf download Qwen/Qwen3.5-9B --local-dir /workspace/models/Qwen3.5-9B > /workspace/dl-9b.log 2>&1
du -sh /workspace/models/Qwen3.5-9B | cut -f1
echo "== $(date) BOOTSTRAP DONE"
