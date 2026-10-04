"""BU Bench V2 as a verifiers taskset, judged by the DeepSeek verifier (reward/verifier.py).

Split: a fixed list of held-out task ids is the validation set (the ten tasks every baseline so far ran on);
everything else is training. The agent sees only the task text (plus the skill document as system prompt and
today's date / time budget); the rubric and weights ride on TaskData for the reward only.

Budgets come from one number, `task.minutes`: it is what the policy is told, the agent timeout is that plus a
minute of grace, and the scoring timeout is derived from the judge settings (retries, per-call timeout, and the
sibling wait when rollouts are judged as a group). Do not set `env.agent.timeout.rollout` in the TOML: it
overrides the agent timeout and the budgets disagree again.

Judging: `task.judge_group = N` sends the N rollouts of one task to the judge in ONE call (comparative judging,
consistent scores within a group); 1 judges each rollout alone. The judge reads every branch of the trace, so a
rollout that pi compacted mid-way is still judged on its whole trajectory.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import verifiers.v1 as vf
from pydantic import Field
from verifiers.v1.types import content_text

from jev_browser.toolset import JevBrowserSharedToolset, JevBrowserToolset

REPO = Path(__file__).resolve().parents[2]  # repo root
HELD_OUT = [
    "bu2-119",
    "bu2-104",
    "bu2-162",
    "bu2-187",
    "bu2-057",
    "bu2-173",
    "bu2-079",
    "bu2-164",
    "bu2-129",
    "bu2-183",
]
# A Jev API refusal is billing or auth, never transient: a rollout that saw one could not navigate at all. It is
# failed as an environment error (excluded from training, no judge call) instead of being scored.
JEV_REFUSED = re.compile(r"Model provider returned HTTP (401|402|403)")
NO_FINAL_ANSWER = (
    "NO FINAL ANSWER. The episode ended (time budget exhausted or the agent was stopped) while the policy was "
    "still calling tools, so it never wrote an answer. Any earlier text was narration between tool calls, not an "
    "answer."
)
_VERIFIER = None
_SYSTEM_PROMPT = None


def _verifier():
    """reward/verifier.py is a script, not a package; import it by path once per process."""
    global _VERIFIER
    if _VERIFIER is None:
        spec = importlib.util.spec_from_file_location("jev_verifier", REPO / "reward" / "verifier.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.load_env()
        _VERIFIER = mod
    return _VERIFIER


def _judge_system_prompt() -> str:
    """The judge's system prompt (rules + appendix, ~115 KB of files): assembled once per process."""
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = _verifier().system_prompt()
    return _SYSTEM_PROMPT


def _text(content) -> str:
    return content if isinstance(content, str) else content_text(content)


def load_bench(enc_path: Path) -> list[dict]:
    from cryptography.fernet import Fernet

    key = base64.urlsafe_b64encode(hashlib.sha256(b"BU_Bench_V2").digest())
    data = json.loads(Fernet(key).decrypt(base64.b64decode(enc_path.read_text())))
    return data["tasks"]


class BatchFailed(Exception):
    """The batched judge call failed; the member should judge itself alone."""


class JudgeBatcher:
    """Judge several rollouts of one task in a single call.

    The rollouts of a task's group are sampled together and finish within one agent budget of each other. The
    first to finish opens a batch under the task's key and waits; the batch is sent when `size` members have
    arrived or `wait` seconds have passed, and each member receives its own findings. A member whose batched call
    fails gets BatchFailed and judges itself alone, so one oversized batch never loses the whole group."""

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    async def judge(self, key: str, member_id: str, payload, size: int, wait: float, run):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        batch = self._open.get(key)
        if batch is None:
            batch = self._open[key] = {"members": [], "run": run, "timer": None}
            batch["timer"] = loop.call_later(wait, self._flush, key)
        batch["members"].append((member_id, payload, future))
        if len(batch["members"]) >= size:
            self._flush(key)
        return await future

    def _flush(self, key: str) -> None:
        batch = self._open.pop(key, None)
        if not batch:
            return
        if batch["timer"] is not None:
            batch["timer"].cancel()
        asyncio.get_running_loop().create_task(self._run(batch["members"], batch["run"]))

    @staticmethod
    async def _run(members, run) -> None:
        try:
            results = await run([(member_id, payload) for member_id, payload, _ in members])
        except Exception as e:  # noqa: BLE001 — every member falls back to a solo call
            for _, _, future in members:
                if not future.done():
                    future.set_exception(BatchFailed(repr(e)[:300]))
            return
        for member_id, _, future in members:
            if future.done():
                continue
            if member_id in results:
                future.set_result(results[member_id])
            else:
                future.set_exception(BatchFailed(f"no findings for {member_id}"))


_BATCHER = JudgeBatcher()


class BenchData(vf.TaskData):
    rubric: str = ""
    weights: dict[str, float] = Field(default_factory=dict)
    title: str = ""


class BenchTaskConfig(vf.TaskConfig):
    judge_model: str = "deepseek-v4-pro"
    judge_effort: str = "high"
    judge_retries: int = 3
    judge_timeout: int = 600
    """Seconds allowed per judge API call."""
    judge_group: int = 1
    """Rollouts of one task judged in a single call. Set it to the training group size; leave 1 for eval."""
    judge_group_wait: float | None = None
    """Seconds the first finished rollout waits for its siblings before judging whoever arrived (default: the
    agent budget, which is the most a sibling can still need)."""
    minutes: float = 15.0
    """Wall-clock budget told to the policy and enforced (plus a minute of grace) as the agent timeout."""
    shared_tools: bool = False
    """True = use the taskset-level shared tool server (tools.url) instead of one server per rollout."""

    @property
    def agent_timeout_s(self) -> float:
        return self.minutes * 60 + 60

    @property
    def group_wait_s(self) -> float:
        return self.judge_group_wait if self.judge_group_wait is not None else self.agent_timeout_s

    @property
    def scoring_timeout_s(self) -> float:
        wait = self.group_wait_s if self.judge_group > 1 else 0
        return wait + self.judge_retries * (self.judge_timeout + 15) + 60


class BenchTask(vf.Task[BenchData, vf.State, BenchTaskConfig]):
    @property
    def key(self) -> str:
        return self.data.id or self.hash

    @classmethod
    def toolsets(cls, config: BenchTaskConfig) -> list[vf.Toolset]:
        """Per-rollout tool server, colocated in the rollout's runtime (its own process, Chrome and daemon)."""
        if config.shared_tools:
            return []
        return [JevBrowserToolset(vf.ToolsetConfig(colocated=True))]

    def _messages(self, trace: vf.Trace) -> list:
        """Assistant and tool messages across EVERY branch of the trace, in order, each once.

        `trace.messages` is the final branch only: after pi compacts, that branch starts at a summary and the
        earlier tool calls and results are gone, and the judge would grade the facts they produced as fabricated.
        Branches whose system prompt is not ours (pi's own summarization request) are skipped; the recent
        messages pi re-sends after a compaction are deduplicated by tool-call id."""
        branches = getattr(trace, "branches", None)
        if not branches:
            return list(trace.messages)
        marker = (self.data.system_prompt or "").strip()[:200]
        out, seen = [], set()
        for branch in branches:
            msgs = list(branch.messages)
            if marker and msgs and msgs[0].role == "system" and marker not in _text(msgs[0].content):
                continue
            for m in msgs:
                if m.role == "assistant":
                    key = ("a", tuple(tc.id for tc in m.tool_calls or []), _text(m.content) if m.content else "")
                elif m.role == "tool":
                    key = ("t", m.tool_call_id)
                else:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                out.append(m)
        return out

    def _trajectory(self, trace: vf.Trace, clip_limit: int) -> tuple[str, str, str, int, bool]:
        """[step n] CALL/RESULT lines, the final answer (the last assistant message WITHOUT tool calls), the last
        narration (any assistant text), the call count, and whether the episode ended mid tool call."""
        v = _verifier()
        steps, final, narration, pending, n, mid_call = [], "", "", [], 0, False
        for m in self._messages(trace):
            if m.role == "assistant":
                calls = list(m.tool_calls or [])
                for tc in calls:
                    n += 1
                    pending.append((tc.id, n))  # calls awaiting their result, in order
                    name = tc.name.rsplit("browser_", 1)
                    name = f"browser_{name[1]}" if len(name) == 2 else tc.name
                    steps.append(f"[step {n}] CALL {name}({tc.arguments})")
                text = _text(m.content) if m.content else ""
                mid_call = bool(calls)
                if text.strip():
                    narration = text
                    if not calls:
                        final = text
            elif m.role == "tool":
                # Match a result to the earliest unanswered call with its id; tool-call ids from the sampled policy
                # can repeat within a rollout, so a plain id lookup would attribute results to the wrong steps.
                i = next((j for j, (cid, _) in enumerate(pending) if cid == m.tool_call_id), 0 if pending else None)
                k = pending.pop(i)[1] if i is not None else n
                steps.append(f"[step {k}] RESULT: {v.clip(_text(m.content), clip_limit)}")
        return "\n".join(steps), final, narration, n, mid_call

    async def _judge_call(self, members: list[tuple[str, tuple[str, str]]]) -> dict[str, tuple[dict, dict]]:
        """One judge call for `members` [(trace_id, (trajectory, final_answer))] -> {trace_id: (findings, meta)}."""
        v = _verifier()
        cfg = self.config
        ids = [chr(ord("A") + i) for i in range(len(members))]
        items = [(short, traj, final) for short, (_, (traj, final)) in zip(ids, members)]
        today = time.strftime("%A, %B %d, %Y")
        user = v.user_message(self.data.prompt_text, self.data.rubric, today, items)
        system = _judge_system_prompt()
        last = None
        for attempt in range(cfg.judge_retries):
            try:
                findings, meta = await asyncio.to_thread(
                    v.call_deepseek, system, user, cfg.judge_model, cfg.judge_effort, cfg.judge_timeout
                )
                per_id = v.split_findings(findings, ids)
                break
            except Exception as e:  # network / parse / shape; retry with backoff
                last = e
                await asyncio.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(f"judge failed after {cfg.judge_retries} attempts: {last}")
        meta = {**meta, "group_size": len(members)}
        return {trace_id: (per_id[short], meta) for short, (trace_id, _) in zip(ids, members)}

    @vf.reward
    async def judge(self, trace: vf.Trace) -> float:
        v = _verifier()
        cfg = self.config
        size = max(1, cfg.judge_group)
        trajectory, final, narration, n_calls, mid_call = self._trajectory(trace, max(2000, v.RESULT_CLIP // size))
        if mid_call:
            final_text = NO_FINAL_ANSWER + (f"\nLast narration: {narration}" if narration else "")
        else:
            final_text = final
        if JEV_REFUSED.search(trajectory):
            raise RuntimeError(
                "environment failure: the Jev API refused requests (HTTP 401/402/403: credits or key); "
                "the rollout could not navigate and is not judged"
            )
        canary = None
        m = re.search(r"CANARY\s+(\S+)", self.data.rubric or "")
        if m:
            canary = m.group(1).rstrip(".,;")
        canary_leak = bool(canary) and (canary in trajectory or canary in final_text)
        payload = (trajectory, final_text)
        if size > 1:
            try:
                findings, meta = await _BATCHER.judge(
                    self.key, trace.id, payload, size, cfg.group_wait_s, self._judge_call
                )
            except BatchFailed:
                findings, meta = (await self._judge_call([(trace.id, payload)]))[trace.id]
        else:
            findings, meta = (await self._judge_call([(trace.id, payload)]))[trace.id]
        judged = v.judge_score(findings, canary_leak)
        formula = v.reward_from_findings(findings, self.data.weights, canary_leak)
        trace.metrics.update(
            {
                "formula_additive": formula["reward_additive"],
                "formula_multiplicative": formula["reward_multiplicative"],
                "outcome": formula["outcome"],
                "harness_use": formula["harness"],
                "tool_calls": float(n_calls),
                "cheats": float(sum(1 for c in judged["cheats_and_failures"] if c.get("kind") == "cheat")),
                "no_final_answer": float(mid_call),
                "judge_group_size": float(meta.get("group_size") or 1),
                "judge_seconds": float(meta.get("seconds") or 0),
            }
        )
        record = {**judged, "formula": formula, "meta": meta, "findings": findings, "task": self.data.id}
        trace.state.artifacts["verifier.json"] = json.dumps(record, ensure_ascii=False).encode()
        # Trace state does not reach the on-disk trace stream; keep the judge's findings durable for analysis.
        out_dir = Path(os.environ.get("JEV_JUDGE_DIR", "/workspace/prl-runs/judge"))
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{trace.id}.json").write_text(json.dumps(record, ensure_ascii=False))
        except OSError:
            pass
        return judged["reward"]


class JevBrowserConfig(vf.TasksetConfig):
    enc_path: Path = Path("/workspace/bu-benchmark/BU_Bench_V2.enc")
    skill_path: Path = REPO / "policy" / "SKILL.md"
    split: Literal["train", "val", "all"] = "train"
    held_out: list[str] = Field(default_factory=lambda: list(HELD_OUT))
    val_repeats: int = 1
    """Copies of each validation task (ids suffixed #2, #3, ...), so eval reward averages several rollouts per
    task instead of one."""
    task: BenchTaskConfig = BenchTaskConfig()
    tools: vf.SharedToolsetConfig = vf.SharedToolsetConfig()
    screenshots: bool = False
    """Whether the served toolset includes browser_screenshot (keeps the skill text consistent with the tools)."""


def skill_body(path: Path, screenshots: bool = False) -> str:
    """The skill document as the policy's system prompt, adjusted to this deployment: bare tool names (no MCP
    prefix), no operator-facing Setup section, and no screenshot tool unless the policy is image-capable."""
    text = path.read_text()
    if text.startswith("---"):  # strip front matter
        text = text.split("---", 2)[2]
    text = text.split("\n## Setup", 1)[0]
    text = text.replace(
        "All names are prefixed `mcp__jev_browser__` when loaded through MCP.", "Call the tools by exactly these names."
    )
    if not screenshots:
        text = "\n".join(line for line in text.splitlines() if "browser_screenshot" not in line)
    return text.strip()


class JevBrowserTaskset(vf.Taskset[BenchTask, JevBrowserConfig]):
    @classmethod
    def toolsets(cls, config: JevBrowserConfig) -> list[vf.Toolset]:
        # A taskset-level (shared) server only when one is configured by URL; otherwise each rollout gets its own
        # server process (own Chrome, own daemon) through BenchTask.toolsets.
        return [JevBrowserSharedToolset(vf.ToolsetConfig(url=config.tools.url))] if config.tools.url else []

    def load(self) -> Iterator[BenchTask]:
        cfg = self.config
        # The per-rollout tool servers inherit this process's environment; they stamp every result with the time
        # used and left, so the policy can budget without a clock of its own.
        os.environ["JEV_BUDGET_S"] = str(int(cfg.task.minutes * 60))
        _judge_system_prompt()  # load the verifier and assemble its 115 KB system prompt once, not per judge call
        system_prompt = (
            skill_body(cfg.skill_path, screenshots=cfg.screenshots)
            + f"\n\nToday's date is {time.strftime('%A, %B %d, %Y')}. Dates earlier than this are in the past."
            + f"\nYou have a budget of about {cfg.task.minutes:g} minutes of wall-clock time for this task. Every "
            "tool result ends with a `time:` line showing how much is used and left. Deliver your answer before "
            "it runs out: a complete, honest partial answer on time beats an unfinished perfect one."
        )
        held = set(cfg.held_out)
        timeout = vf.TaskTimeout(agent=cfg.task.agent_timeout_s, scoring=cfg.task.scoring_timeout_s)
        for t in load_bench(cfg.enc_path):
            in_val = t["id"] in held
            if (cfg.split == "train" and in_val) or (cfg.split == "val" and not in_val):
                continue
            repeats = max(1, cfg.val_repeats) if (cfg.split == "val" and in_val) else 1
            for r in range(repeats):
                task_id = t["id"] if r == 0 else f"{t['id']}#{r + 1}"
                yield BenchTask(
                    BenchData(
                        id=task_id,
                        name=t.get("title", t["id"]),
                        title=t.get("title", ""),
                        prompt=t["task"],
                        system_prompt=system_prompt,
                        rubric=t.get("rubric", ""),
                        weights=t.get("weights") or {},
                        timeout=timeout,
                    ),
                    cfg.task,
                )
