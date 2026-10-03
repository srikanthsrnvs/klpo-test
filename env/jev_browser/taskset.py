"""BU Bench V2 as a verifiers taskset, judged by the DeepSeek verifier (reward/verifier.py).

Split: a fixed list of held-out task ids is the validation set (the ten tasks every baseline so far ran on);
everything else is training. The agent sees only the task text (plus the skill document as system prompt and
today's date / time budget); the rubric and weights ride on TaskData for the reward only.
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

from jev_browser.toolset import JevBrowserToolset

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


def _verifier():
    """reward/verifier.py is a script, not a package; import it by path once."""
    spec = importlib.util.spec_from_file_location("jev_verifier", REPO / "reward" / "verifier.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.load_env()
    return mod


def load_bench(enc_path: Path) -> list[dict]:
    from cryptography.fernet import Fernet

    key = base64.urlsafe_b64encode(hashlib.sha256(b"BU_Bench_V2").digest())
    data = json.loads(Fernet(key).decrypt(base64.b64decode(enc_path.read_text())))
    return data["tasks"]


class BenchData(vf.TaskData):
    rubric: str = ""
    weights: dict[str, float] = Field(default_factory=dict)
    title: str = ""


class BenchTaskConfig(vf.TaskConfig):
    judge_model: str = "deepseek-v4-pro"
    judge_effort: str = "high"
    judge_retries: int = 3
    minutes: float = 15.0
    """Wall-clock budget told to the policy and enforced as the agent timeout."""
    shared_tools: bool = False
    """True = use the taskset-level shared tool server (tools.url) instead of one server per rollout."""


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

    def _trajectory(self, trace: vf.Trace) -> tuple[str, str, int]:
        """[step n] CALL/RESULT lines from the final branch's messages, plus the final answer."""
        v = _verifier()
        steps, final, ids, n = [], "", {}, 0
        for m in trace.messages:
            if m.role == "assistant":
                for tc in m.tool_calls or []:
                    n += 1
                    ids[tc.id] = n
                    name = tc.name.rsplit("browser_", 1)
                    name = f"browser_{name[1]}" if len(name) == 2 else tc.name
                    steps.append(f"[step {n}] CALL {name}({tc.arguments})")
                if m.content and m.content.strip():
                    final = m.content
            elif m.role == "tool":
                k = ids.get(m.tool_call_id, n)
                text = m.content if isinstance(m.content, str) else content_text(m.content)
                steps.append(f"[step {k}] RESULT: {v.clip(text)}")
        return "\n".join(steps), final, n

    @vf.reward
    async def judge(self, trace: vf.Trace) -> float:
        v = _verifier()
        trajectory, final, n_calls = self._trajectory(trace)
        canary = None
        m = re.search(r"CANARY\s+(\S+)", self.data.rubric or "")
        if m:
            canary = m.group(1).rstrip(".,;")
        canary_leak = bool(canary) and (canary in trajectory or canary in final)
        today = time.strftime("%A, %B %d, %Y")
        user = v.user_message(self.data.prompt_text, self.data.rubric, today, trajectory, final)
        system = v.system_prompt()
        last = None
        for attempt in range(self.config.judge_retries):
            try:
                findings, meta = await asyncio.to_thread(
                    v.call_deepseek, system, user, self.config.judge_model, self.config.judge_effort
                )
                break
            except Exception as e:  # network / parse; retry with backoff
                last = e
                await asyncio.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(f"judge failed after {self.config.judge_retries} attempts: {last}")
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
        return [JevBrowserToolset(vf.ToolsetConfig(url=config.tools.url))] if config.tools.url else []

    def load(self) -> Iterator[BenchTask]:
        cfg = self.config
        system_prompt = (
            skill_body(cfg.skill_path, screenshots=cfg.screenshots)
            + f"\n\nToday's date is {time.strftime('%A, %B %d, %Y')}. Dates earlier than this are in the past."
            + f"\nYou have a budget of about {cfg.task.minutes:g} minutes of wall-clock time for this task. Deliver "
            "your answer before it runs out: a complete, honest partial answer on time beats an unfinished perfect one."
        )
        held = set(cfg.held_out)
        for t in load_bench(cfg.enc_path):
            in_val = t["id"] in held
            if (cfg.split == "train" and in_val) or (cfg.split == "val" and not in_val):
                continue
            yield BenchTask(
                BenchData(
                    id=t["id"],
                    name=t.get("title", t["id"]),
                    title=t.get("title", ""),
                    prompt=t["task"],
                    system_prompt=system_prompt,
                    rubric=t.get("rubric", ""),
                    weights=t.get("weights") or {},
                    timeout=vf.TaskTimeout(agent=cfg.task.minutes * 60 + 60, scoring=1200),
                ),
                cfg.task,
            )
