"""Per-step summary of a prime-rl run from monitors/file/metrics.jsonl: train reward, loss, KLPO stats, episode
shape, eval results. Usage: python summarize_run.py /workspace/prl-runs/<run>"""

import json
import sys
from pathlib import Path

run = Path(sys.argv[1])
rows = [json.loads(l) for l in open(run / "monitors/file/metrics.jsonl")]
by_step = {}
for r in rows:
    step = r.get("step")
    if step is None:
        continue
    d = by_step.setdefault(step, {})
    for k, v in r.items():
        if k != "step":
            d[k] = v


def g(d, *keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


print(f"{'step':>4} {'train_rew':>9} {'timeout':>7} {'err':>5} {'turns':>6} {'out_tok':>7} {'loss':>8} {'klpo_R':>7} {'logratio':>9} {'eval_rew':>8} {'eval_timeout':>12}")
for step in sorted(by_step):
    d = by_step[step]
    tr = g(d, "train/agg/all/agent/reward/mean")
    to = g(d, "train/agg/all/agent/is_timeout/mean")
    err = g(d, "train/agg/all/is_error/mean", "train/agg/all/agent/is_error/mean")
    turns = g(d, "train/agg/all/agent/num_turns/mean")
    out = g(d, "train/agg/all/agent/num_output_tokens/mean")
    loss = g(d, "loss/mean")
    kr = g(d, "klpo/reward/mean")
    lr = g(d, "klpo/log_ratio_mean/mean")
    ev = g(d, "eval/jev-browser-val/all/agent/reward/mean")
    evt = g(d, "eval/jev-browser-val/all/agent/is_timeout/mean")
    f = lambda x, w=0: "" if x is None else (f"{x:.3f}" if isinstance(x, float) else str(x))
    if any(v is not None for v in (tr, loss, ev)):
        print(f"{step:>4} {f(tr):>9} {f(to):>7} {f(err):>5} {f(turns):>6} {f(out):>7} {f(loss):>8} {f(kr):>7} {f(lr):>9} {f(ev):>8} {f(evt):>12}")
