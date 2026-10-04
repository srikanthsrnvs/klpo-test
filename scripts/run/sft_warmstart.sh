#!/bin/sh
# SFT warm start from DeepSeek-policy trajectories (configs/collect-sft-deepseek.toml must have run):
#   1. convert the run's traces.jsonl to a HF dataset (prime-rl's converter: one row per branch, messages + tools)
#   2. keep rows the judge scored >= MIN_REWARD with no error
#   3. full fine-tune Qwen3.5-9B on them (configs/sft-deepseek.toml; needs all 8 GPUs: stop the RL run first)
#   4. export the last checkpoint to bf16 HF weights at /workspace/models/Qwen3.5-9B-sft (rl-v4's model.name)
# Usage: RUN=sft-deepseek MIN_REWARD=0.5 sh scripts/run/sft_warmstart.sh     Log: /workspace/prl-runs/sft-warmstart.log
export PATH="$HOME/.local/bin:$PATH"
set -e
RUN="${RUN:-sft-deepseek}"; MIN_REWARD="${MIN_REWARD:-0.5}"; OUT=/workspace/models/Qwen3.5-9B-sft
TRACES="/workspace/vf-runs/$RUN/traces.jsonl"; DATA=/workspace/sft-data/deepseek
[ -s "$TRACES" ] || { echo "no traces at $TRACES"; exit 1; }
pgrep -f "[r]l @ /workspace" >/dev/null && { echo "the RL run is still running; stop it first (scripts/run/stop_all.sh)"; exit 1; }
cd /workspace/prime-rl
echo "== convert"; rm -rf "$DATA"; uv run --no-sync python tools/convert_traces_to_hf_dataset.py "$TRACES" --name "$DATA" --local | tail -2
echo "== filter (reward >= $MIN_REWARD, no error)"; uv run --no-sync python - "$DATA" "$MIN_REWARD" <<'PY'
import sys
from datasets import load_dataset
data, lo = sys.argv[1], float(sys.argv[2])
ds = load_dataset(data, "default", split="train")
good = ds.filter(lambda r: (r["reward"] or 0.0) >= lo and not r.get("has_error"))
print(f"rows: {len(ds)} -> {len(good)} kept; mean reward kept {sum(r['reward'] for r in good)/max(len(good),1):.2f}")
assert len(good) >= 8, "too few good trajectories for SFT"
good.to_parquet(f"{data}/default/train.parquet")
PY
KEPT=$(uv run --no-sync python -c "from datasets import load_dataset; print(len(load_dataset('$DATA','default',split='train')))")
STEPS=$(( (KEPT * 2 + 7) / 8 )); [ "$STEPS" -lt 10 ] && STEPS=10
echo "== sft: $KEPT rows, $STEPS steps (~2 epochs of 8)"; uv run --no-sync sft @ /workspace/jev-browser-rl/configs/sft-deepseek.toml --clean --max_steps "$STEPS" --ckpt.interval "$STEPS" 2>&1 | grep -v "UserWarning\|_pytree" | tail -15
STEP=$(ls -d /workspace/prl-runs/sft-deepseek/checkpoints/step_* | sed -E "s/.*step_//" | sort -n | tail -1)
echo "== export step $STEP -> $OUT"; rm -rf "$OUT"; uv run --no-sync torchrun --nproc-per-node 8 tools/convert_dcp_to_bf16.py "/workspace/prl-runs/sft-deepseek/checkpoints/step_$STEP" "$OUT" 2>&1 | tail -3
ls "$OUT" | head; [ -f "$OUT/config.json" ] && echo "SFT WARMSTART DONE: $OUT" || { echo "export failed"; exit 1; }
