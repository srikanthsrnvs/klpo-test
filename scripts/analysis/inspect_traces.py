"""Summarize a verifiers traces.jsonl: per trace, reward, metrics, token counts, whether sampler logprobs were
recorded (needed for KLPO), tool calls made, and any error. Usage: python inspect_traces.py <run-dir or traces.jsonl>"""

import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
p = p / "traces.jsonl" if p.is_dir() else p
for line in open(p):
    ep = json.loads(line)
    task = ep.get("task", {})
    print(f"episode {ep['id'][:8]} task={task.get('data', {}).get('id') or task.get('key')} ok={ep.get('ok')} "
          f"timeout={ep.get('is_timeout')} tokens in/out={ep.get('num_input_tokens')}/{ep.get('num_output_tokens')}")
    for err in ep.get("errors") or []:
        print("  ERROR:", err.get("type"), (err.get("message") or "")[:300])
    for tr in ep.get("traces") or []:
        rewards = {k: (v.get("value") if isinstance(v, dict) else v) for k, v in (tr.get("rewards") or {}).items()}
        print(f"  trace {tr['id'][:8]} agent={tr.get('agent', {}).get('name')} reward={tr.get('reward')} rewards={rewards}")
        print(f"    metrics={ {k: (round(v, 3) if isinstance(v, float) else v) for k, v in (tr.get('metrics') or {}).items()} }")
        print(f"    stop={tr.get('stop_condition')} error={(tr.get('error') or {}).get('message', '')[:200] if tr.get('error') else None}")
        branches = tr.get("branches") or []
        calls, sampled, with_lp, nodes = [], 0, 0, 0
        for b in branches:
            for n in b.get("nodes") or []:
                nodes += 1
                m = n.get("message") or {}
                for tc in m.get("tool_calls") or []:
                    calls.append(tc.get("name"))
                if n.get("sampled") or any(n.get("mask") or []):
                    sampled += 1
                    if n.get("logprobs"):
                        with_lp += 1
        print(f"    branches={len(branches)} nodes={nodes} sampled_nodes={sampled} with_logprobs={with_lp} tool_calls={len(calls)}")
        print(f"    calls: {calls[:30]}")
        for b in branches[-1:]:
            msgs = [n.get("message") or {} for n in b.get("nodes") or []]
            last = [m for m in msgs if m.get("role") == "assistant" and m.get("content")]
            if last:
                print("    final:", last[-1]["content"][:400].replace("\n", " "))
