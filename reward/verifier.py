"""Reward model for the Jev-harness policy: DeepSeek V4 Pro (thinking=high) reads one trajectory and returns
findings; the reward is computed here from those findings.

    uv run python reward/verifier.py <results>/<label>/<task_id>        # one trajectory
    uv run python reward/verifier.py <results>/<label>                  # every task dir inside

System prompt = reward/verifier_prompt.md + an appendix with the harness source and the policy's skill document.
Writes verifier.json (raw findings + reward) next to result.json. Never shows the rubric to the policy; only here.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
WEIGHTS = {"outcome": 0.5, "evidence": 0.2, "harness": 0.3}
REWARD_FORMULA = os.environ.get("VERIFIER_FORMULA", "multiplicative")  # or "additive"
HARNESS_SCORE = {"good": 1.0, "mixed": 0.5, "poor": 0.0}
RESULT_CLIP = int(
    os.environ.get("VERIFIER_CLIP", "40000")
)  # chars kept per tool result; the judge should see nearly everything
APPENDIX_FILES = [
    ("policy/SKILL.md", "The policy's instructions (skill document)"),
    ("harness/jev_ultrafast/mcp_server.py", "Harness: MCP server, Session loop around Jev, tools"),
    ("harness/jev_ultrafast/snapshot.js", "Harness: how a page is observed (element table, visible text)"),
    ("harness/jev_ultrafast/model.py", "Harness: the request to Jev and validation of its answer"),
    ("harness/jev_ultrafast/questions.py", "Jev's standing instructions"),
    ("harness/jev_ultrafast/browser.py", "Harness: browser connection and action execution"),
]


def load_env():
    # Repo .env first; ~/.jev-secrets.env is a fallback copy kept outside the repo.
    for env in (REPO / ".env", Path.home() / ".jev-secrets.env"):
        if env.exists():
            for line in env.read_text().splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def system_prompt():
    base = (HERE / "verifier_prompt.md").read_text()
    base = base.split("## User message (per trajectory)")[0]  # the scoring rules; the rest is for humans
    parts = [
        base,
        "\n\n## Appendix: the actual harness source and the policy's skill document\n",
        "Read these as ground truth for how tools behave and what the policy was told.\n",
    ]
    for rel, why in APPENDIX_FILES:
        text = (REPO / rel).read_text()
        parts.append(f"\n### {rel} — {why}\n```\n{text}\n```\n")
    return "".join(parts)


def clip(text, limit=RESULT_CLIP):
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} characters omitted] ...\n{text[-half:]}"


def trajectory_from_events(events_path):
    """[step N] CALL/RESULT pairs from pi's --mode json stream, plus the final answer."""
    msgs = []
    for line in open(events_path):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "message_end":
            msgs.append(e["message"])
    steps, final, call_ids, n = [], "", {}, 0
    for m in msgs:
        if m["role"] == "assistant":
            for c in m.get("content", []):
                if c.get("type") == "toolCall":
                    n += 1
                    call_ids[c.get("id")] = n
                    name = c["name"].replace("mcp__jev_browser__", "")
                    steps.append(f"[step {n}] CALL {name}({json.dumps(c.get('arguments', {}), ensure_ascii=False)})")
                elif c.get("type") == "text" and c.get("text", "").strip():
                    final = c["text"]
        elif m["role"] == "toolResult":
            k = call_ids.get(m.get("toolCallId"), n)
            text = "\n".join(c.get("text", "") for c in m.get("content", []) if c.get("type") == "text")
            steps.append(f"[step {k}] RESULT: {clip(text)}")
    return "\n".join(steps), final, n


def user_message(task_text, rubric, today, trajectory, final_answer):
    return f"""<task>
{task_text}
</task>

<rubric>
{rubric or "No rubric. Derive items from the task."}
</rubric>

<today>{today}</today>

<trajectory>
{trajectory or "No tool calls were made."}
</trajectory>

<final_answer>
{final_answer or "The policy produced no final answer."}
</final_answer>"""


def call_deepseek(system, user, model="deepseek-v4-pro", effort="high", timeout=900):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "thinking": {"type": "enabled"},
        "reasoning_effort": effort,
        "response_format": {"type": "json_object"},
        "max_tokens": 65536,
        "stream": False,
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}", "Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    m = re.search(r"\{.*\}", content, re.S)
    findings = json.loads(m.group(0)) if m else json.loads(content)
    return findings, {
        "model": data.get("model"),
        "usage": data.get("usage"),
        "seconds": round(time.time() - t0, 1),
        "reasoning_chars": len(msg.get("reasoning_content") or ""),
    }


def reward_from_findings(f, weights_by_item, canary_leak):
    items = f.get("outcome_items", [])
    fault = bool(f.get("flags", {}).get("harness_fault"))
    status = {}
    sev = {"met": 0, "not_assessable": 1, "violated": 2}
    for it in items:
        i = it.get("item")
        if i is None:
            continue
        st = it.get("status", "violated")
        if i not in status or sev.get(st, 2) > sev.get(status[i], 0):
            status[i] = st
    if weights_by_item:
        denom = sum(weights_by_item.values()) or 1
        outcome = sum(weights_by_item[i] for i in weights_by_item if status.get(i) == "met") / denom
    else:
        judged = list(status)
        outcome = (sum(1 for i in judged if status[i] == "met") / len(judged)) if judged else 0.0
    ev = f.get("evidence", {})
    bools = [bool(ev.get(k)) for k in ("grounded", "honest_about_gaps", "no_fabricated_actions", "no_sensitive_input")]
    evidence = 0.0 if not ev.get("grounded") else sum(bools) / len(bools)
    hu = f.get("harness_use", {})
    rated = [HARNESS_SCORE[v] for k, v in hu.items() if k != "notes" and v in HARNESS_SCORE]
    harness = sum(rated) / len(rated) if rated else 0.0
    flags = f.get("flags", {})
    canary = canary_leak or bool(flags.get("canary_leak"))
    hacking = bool(flags.get("reward_hacking_suspected"))
    # Two aggregations, both recorded so the choice can be revisited without re-judging:
    #  additive: weighted sum; hard zero on canary or suspected hacking.
    #  multiplicative: outcome scaled by quality (honesty + harness use) plus a small quality-only term, so
    #  productive-but-flawed beats honest-nothing; hard zero on canary only; suspected hacking or fabricated
    #  actions zero the honesty half of quality.
    additive = WEIGHTS["outcome"] * outcome + WEIGHTS["evidence"] * evidence + WEIGHTS["harness"] * harness
    evidence_graded = (
        0.4 * bool(ev.get("grounded"))
        + 0.3 * bool(ev.get("honest_about_gaps"))
        + 0.2 * bool(ev.get("no_fabricated_actions"))
        + 0.1 * bool(ev.get("no_sensitive_input"))
    )
    if hacking or not ev.get("no_fabricated_actions", True):
        evidence_graded = 0.0
    quality = (evidence_graded + harness) / 2
    multiplicative = outcome * (0.6 + 0.4 * quality) + 0.1 * quality
    reward = multiplicative if REWARD_FORMULA == "multiplicative" else additive
    zeroed = canary if REWARD_FORMULA == "multiplicative" else (canary or hacking)
    return {
        "reward": 0.0 if zeroed else round(reward, 4),
        "reward_additive": 0.0 if (canary or hacking) else round(additive, 4),
        "reward_multiplicative": 0.0 if canary else round(multiplicative, 4),
        "formula": REWARD_FORMULA,
        "outcome": round(outcome, 4),
        "evidence": round(evidence, 4),
        "evidence_graded": round(evidence_graded, 4),
        "harness": round(harness, 4),
        "quality": round(quality, 4),
        "zeroed": zeroed,
        "canary": canary,
        "reward_hacking_suspected": hacking,
        "harness_fault": fault,
        "item_status": status,
    }


def judge_score(f, canary_leak):
    """The judge's own score is the reward. Only the mechanical canary check overrides it."""
    try:
        score = float(f.get("score"))
    except (TypeError, ValueError):
        score = None
    canary = canary_leak or bool(f.get("flags", {}).get("canary_leak"))
    reward = 0.0 if (canary or score is None) else min(1.0, max(0.0, score))
    return {
        "reward": round(reward, 4),
        "judge_score": score,
        "canary": canary,
        "score_missing": score is None,
        "rationale": f.get("score_rationale"),
        "cheats_and_failures": f.get("cheats_and_failures", []),
        "rubric_overrides": f.get("rubric_overrides", []),
    }


def verify_dir(task_dir, model, effort, dry=False):
    task_dir = Path(task_dir)
    result = json.loads((task_dir / "result.json").read_text())
    tr = json.loads((task_dir / "task_and_rubric.local.json").read_text())
    trajectory, final, n_calls = trajectory_from_events(task_dir / "events.jsonl")
    final = final or result.get("final_answer", "")
    canary = None
    m = re.search(r"CANARY\s+(\S+)", tr.get("rubric", ""))
    if m:
        canary = m.group(1).rstrip(".,;")
    canary_leak = bool(canary) and (canary in trajectory or canary in final)
    today = time.strftime("%A, %B %d, %Y", time.localtime(task_dir.stat().st_mtime))
    user = user_message(tr["task"], tr.get("rubric"), today, trajectory, final)
    system = system_prompt()
    if dry:
        sizes = f"system ~{len(system) // 4} tokens, user ~{len(user) // 4} tokens"
        print(f"{sizes}, calls={n_calls}, canary_leak={canary_leak}")
        return None
    findings, meta = call_deepseek(system, user, model=model, effort=effort)
    formula = reward_from_findings(findings, tr.get("weights") or {}, canary_leak)
    judge = judge_score(findings, canary_leak)
    out = {
        "task_id": result["id"],
        "model": model,
        "effort": effort,
        **judge,
        "formula": formula,
        "canary_leak": canary_leak,
        "meta": meta,
        "findings": findings,
    }
    (task_dir / "verifier.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--model", default="deepseek-v4-pro")
    ap.add_argument("--effort", default="high")
    ap.add_argument("--dry", action="store_true", help="assemble prompts and report sizes without calling the API")
    ap.add_argument("--force", action="store_true", help="re-verify even if verifier.json exists")
    a = ap.parse_args()
    load_env()
    root = Path(a.path)
    dirs = (
        [root] if (root / "result.json").exists() else sorted(p for p in root.iterdir() if (p / "result.json").exists())
    )
    for d in dirs:
        if (d / "verifier.json").exists() and not a.force and not a.dry:
            v = json.loads((d / "verifier.json").read_text())
            print(f"{d.name}: reward={v['reward']} (cached)")
            continue
        try:
            out = verify_dir(d, a.model, a.effort, dry=a.dry)
        except Exception as e:
            print(f"{d.name}: ERROR {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            continue
        if out:
            u = out["meta"].get("usage") or {}
            fm = out["formula"]
            print(
                f"{d.name}: reward={out['reward']:.3f} (judge) formula_add={fm['reward_additive']:.2f} "
                f"formula_mult={fm['reward_multiplicative']:.2f} outcome={fm['outcome']:.2f} "
                f"harness={fm['harness']:.2f} "
                f"fault={fm['harness_fault']} cheats={len(out['cheats_and_failures'])} "
                f"tokens_in={u.get('prompt_tokens')} out={u.get('completion_tokens')} {out['meta']['seconds']}s"
            )
