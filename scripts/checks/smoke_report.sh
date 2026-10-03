#!/bin/sh
# Summarize the smoke eval: processes, tool-server failures, per-episode tools offered / calls / not-found / reward.
sh /workspace/jev-browser-rl/scripts/ops/status_procs.sh
echo "=== tool-server failure logs"; ls /workspace/vf-runs/tool-servers/ 2>/dev/null | head -5
for f in $(ls /workspace/vf-runs/tool-servers/*.log 2>/dev/null | head -2); do echo "--- $f"; grep -v "^  File\|^    " "$f" | head -25 | cut -c1-300; done
echo "=== eval log"; grep -E "Evaluated|rollout done|ERROR|tool server" /workspace/vf-runs/smoke-deepseek/logs/latest/eval.log 2>/dev/null | tail -8 | cut -c1-200
[ -f /workspace/vf-runs/smoke-deepseek/traces.jsonl ] && /workspace/prime-rl/.venv/bin/python - <<PY
import json,collections
for line in open("/workspace/vf-runs/smoke-deepseek/traces.jsonl"):
    d=json.loads(line)
    for t in d.get("traces") or []:
        nodes=t.get("nodes") or []
        tools=[x.get("name") or (x.get("function") or {}).get("name") for x in (nodes[0].get("tools") or [])] if nodes else []
        calls=collections.Counter(tc["name"].split("__")[-1] for nd in nodes for tc in ((nd.get("message") or {}).get("tool_calls") or []))
        nf=sum(1 for nd in nodes if (nd.get("message") or {}).get("role")=="tool" and "not found" in str((nd.get("message") or {}).get("content")))
        print("task", d.get("task",{}).get("data",{}).get("id"), "tools_offered", len(tools), "calls", dict(calls), "not_found", nf, "reward", t.get("rewards"), "stop", t.get("stop_condition"), "errors", [(e.get("type"), (e.get("message") or "")[:160]) for e in (t.get("errors") or [])])
PY
