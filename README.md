# jev-browser-rl

Reinforcement learning for a small language model that drives a browser through the **jev-browser** harness.

The policy (Qwen3.5-9B with a LoRA adapter) never clicks. It reads page text, writes one goal at a time, and hands
each goal to **Jev**, a fast browser-navigation model (TypeSafe API) that picks the clicks, typing and scrolling
itself. The policy answers Jev's questions, judges when the task is done, and writes the final answer. Tasks come
from **BU Bench V2** (real websites). A **DeepSeek judge** reads each full trajectory and returns a score in
[0, 1] that is the reward. Training runs on **prime-rl** with a custom **KLPO** (binary-KL) loss.

## One training step

```
prime-rl orchestrator
  │  samples 8 tasks × 4 rollouts from the `jev-browser` taskset (190 train / 10 val)
  ▼
rollout (verifiers v1, `jev-pi` harness = pi agent over ACP, subprocess runtime)
  │  system prompt = policy/SKILL.md (adapted by skill_body) + today's date + 15-minute budget
  │  policy served by vLLM (LoRA), sampler log-probs recorded per token
  │  tools = a private MCP tool server per rollout (own headless Chrome + browser-harness daemon)
  │          browser_open / run / type / back / goto / text / wait / close
  │  browser_run(goal) → Jev chooses one action per step on the live page until done / needs_input / blocked / budget
  ▼
reward (reward/verifier.py, called from BenchTask.judge)
  │  DeepSeek deepseek-v4-pro (thinking, effort high) reads task + rubric + full trajectory + final answer
  │  reward = judge's score, clamped to [0, 1]; forced to 0 if the rubric's canary string leaks
  ▼
advantage (prime-rl `grpo`): reward − mean reward of the task's group of 4
  ▼
trainer: LoRA r8 on Qwen3.5-9B, loss = trainer/klpo_prime.py (KLPO binary KL, β = 0.1)
  │  new adapter weights broadcast to vLLM through the filesystem
```

## What to review

| File | What it is |
| --- | --- |
| [policy/SKILL.md](policy/SKILL.md) | The policy's instructions. `skill_body()` turns it into the system prompt (see design doc). |
| [reward/verifier_prompt.md](reward/verifier_prompt.md) | The judge's scoring rules (sections A–D, output JSON, score anchors). |
| [reward/verifier.py](reward/verifier.py) | Builds the judge prompt, calls DeepSeek, turns findings into the reward. |
| [env/jev_browser/taskset.py](env/jev_browser/taskset.py) | Taskset: loads BU Bench V2, train/val split, system prompt, `@vf.reward` judge. |
| [env/jev_browser/toolset.py](env/jev_browser/toolset.py) | The 8 browser tools as a verifiers `Toolset`; per-rollout Chrome lifecycle. |
| [env/jev_pi/\_\_init\_\_.py](env/jev_pi/__init__.py) | `jev-pi` harness: pi context window, compaction, tool-cache pre-seed. |
| [trainer/klpo_prime.py](trainer/klpo_prime.py) | The custom loss plugged into prime-rl. |
| [trainer/prime-rl.patch](trainer/prime-rl.patch) | Changes to prime-rl (adds the `raw` advantage algorithm). Not used by Run 2, which uses `grpo`. |
| [configs/rl-v2-9b.toml](configs/rl-v2-9b.toml) | The current training config (Run 2). |
| [harness/](harness/) | The jev-ultrafast browser harness (MIT, Browser Use) with the changes this project needs. |

## Layout

```
policy/      SKILL.md — policy instructions (training prompt)
reward/      verifier.py, verifier_prompt.md — the judge (training prompt + reward code)
env/         verifiers environment package `jev-browser` (packages jev_browser, jev_pi)
trainer/     KLPO loss, prime-rl patch + idempotent patcher, trainer smoke test
configs/     prime-rl `rl` and `vf-eval` TOMLs
harness/     jev-ultrafast harness (MCP server `jev_ultrafast/mcp_server.py`); upstream.patch = diff vs upstream
scripts/
  setup/     bootstrap_box.sh — fresh GPU box: Chrome, prime-rl, patch, packages, model
  checks/    preflight.sh and its parts (rollout-server test, tool load test, GPU-free smoke eval)
  run/       start / watch / resume / stop the run, shared tool server, dashboard
  ops/       process status, browser cleanup
  analysis/  summarize_run.py (per-step metrics), inspect_traces.py (per-trace tool use, rewards)
```

## Running it

The scripts assume a single Linux GPU box (Run 2 used 4× A100-80GB) with this repo at `/workspace/jev-browser-rl`,
prime-rl at `/workspace/prime-rl`, runs under `/workspace/prl-runs`, and the model at `/workspace/models/Qwen3.5-9B`.

1. **Secrets.** Copy `.env.example` to `/workspace/jev-browser-rl/.env` and fill it in (TypeSafe key for Jev,
   a text-model key for Jev's typed values, DeepSeek key for the judge). `.env` is git-ignored.
2. **Benchmark.** Put `BU_Bench_V2.enc` (BU Bench V2, Fernet-encrypted) at `/workspace/bu-benchmark/BU_Bench_V2.enc`,
   or set `env.taskset.enc_path`.
3. **Bootstrap.** `bash scripts/setup/bootstrap_box.sh` installs Chrome and a headless wrapper, clones prime-rl,
   applies `trainer/patch_prime_rl.py`, copies the loss into prime-rl's `src/`, installs `harness/` and `env/`
   into prime-rl's venv, runs `trainer/smoke_prime.py`, and downloads the model. The code here was run against
   prime-rl commit `4c7c0d5d188ef93d5fc782b41863870cf6dc0c99`; check that commit out before patching to reproduce.
4. **Preflight.** `sh scripts/checks/preflight.sh` checks Chrome, the loss/algorithm smoke test, taskset loading,
   per-rollout tool servers, the judge API, and a 4-episode end-to-end smoke eval with DeepSeek as the policy.
5. **Launch.** `RUN_NAME=jev9b-v2 MODEL_CFG=rl-v2-9b.toml sh scripts/run/start_rl.sh`
   (`CLEAN=1` deletes the run directory first; `RESUME_STEP=N` resumes from a checkpoint).
   Always pass `RUN_NAME` and `MODEL_CFG`: the scripts' defaults refer to older configs.
6. **Watch.** Logs are under `/workspace/prl-runs/<run>/logs/attempt_<n>/`. `scripts/run/start_dashboard.sh` serves
   prime-rl's dashboard on 127.0.0.1:8080. `python scripts/analysis/summarize_run.py /workspace/prl-runs/<run>`
   prints per-step reward, loss and KLPO metrics. Per-rollout judge findings are written to
   `/workspace/prl-runs/judge/<trace_id>.json`.
7. **Stop.** `sh scripts/run/stop_all.sh` (keeps checkpoints). `scripts/run/watch_rl.sh` is an optional watchdog
   that relaunches from the newest complete checkpoint.

## Known issues

- **Run 2 trainer crash.** The trainer failed on its first optimizer step with
  `RuntimeError: mean(): could not infer output dtype ... Got: Int` in `Qwen3_5RMSNorm.forward`
  (`prime_rl/trainer/models/qwen3_5/norm.py:20`), inside activation-checkpoint recompute: the norm received an
  int32 tensor (`[0, 99650, 196671, 261944]`, which looks like packed-sequence offsets) instead of hidden states.
  Config at the time: `cp = 2` (Ulysses), activation checkpointing with offloading, `seq_len = 262144`.
  Root cause not yet determined. Before the crash: step-0 validation reward 0.141 (10 tasks), first training batch
  reward 0.108 (32 rollouts).
- **The judge reads a different skill document than the policy.** `reward/verifier.py` appends the raw
  `policy/SKILL.md` to the judge prompt, while the policy sees the `skill_body()` version (no Setup section, no
  `browser_screenshot` lines, tool-prefix sentence replaced). The judge prompt also lists `browser_type(..., submit)`
  and `browser_text(..., max_chars)` parameters that the policy's tool table does not mention.
- **Dates.** The policy's "today" is fixed when the taskset loads; the judge's `<today>` is taken at judging time.
  They differ for rollouts that run across midnight.
- **Missing judge score becomes reward 0.** If the judge returns JSON without a usable `score`, `judge_score()`
  sets the reward to 0 (flagged `score_missing`). Network or JSON-parse failures are retried 3 times and then fail
  the rollout instead.
- **Judge cost.** The judge system prompt is about 115 KB (scoring rules plus harness source) on every call, and
  each tool result in the trajectory can be up to 40,000 characters (`VERIFIER_CLIP`).
- **`start_rl.sh` kills the dashboard.** Its cleanup loop runs `pkill -9 -f "PRL::"`, which also matches the
  dashboard process (`PRL::Dashboard`). `watch_rl.sh` restarts it; a direct `start_rl.sh` launch does not.

## License

`harness/` is jev-ultrafast by Browser Use, MIT licensed (see [harness/LICENSE](harness/LICENSE)); the changes
made here are listed in [harness/upstream.patch](harness/upstream.patch).
