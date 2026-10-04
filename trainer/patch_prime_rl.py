"""Apply this repo's prime-rl patches to a checkout (idempotent). Usage: python patch_prime_rl.py /workspace/prime-rl

1. the `raw` advantage algorithm (trainer/prime-rl.patch, applied by string edits below)
2. the Qwen3.5 Ulysses context-parallel fix (trainer/prime-rl-ulysses-cp.patch, applied with `git apply`): FLA's
   memoized CP context was built inside activation-checkpointed layers, so the backward recompute skipped its
   CUDA->CPU copies and selective checkpointing served the post-attention norm the saved int32 cu_seqlens
   ("mean(): could not infer output dtype ... Got: Int"). The context is now built once per forward, outside the layers.
"""
import pathlib
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
cfg = root / "packages/prime-rl-configs/src/prime_rl/configs/algorithm.py"
s = cfg.read_text()
if "RawRewardAlgoConfig" not in s:
    block = '''

class RawRewardAlgoConfig(BaseAlgoConfig):
    type: Literal["raw"] = "raw"
    """Raw-reward credit: every sampled token of a clean trainable trace gets
    that trace's terminal reward (scaled, shifted) as its advantage, with no
    baseline. For critic-free single-rollout objectives such as KLPO token
    regression, whose custom loss builds its own return coefficient from R and
    the log-ratio. Works with any ``group_size`` (including 1)."""

    action_loss_type: ClassVar[ActionLossType] = "rl"

    scale: float = Field(1.0, allow_inf_nan=False)
    shift: float = Field(0.0, allow_inf_nan=False)

'''
    s = s.replace("\n\nAlgoConfig: TypeAlias = Annotated[", block + "\nAlgoConfig: TypeAlias = Annotated[", 1)
    s = s.replace(
        '    | DebugAlgoConfig,\n    Field(discriminator="type"),',
        '    | DebugAlgoConfig\n    | RawRewardAlgoConfig,\n    Field(discriminator="type"),',
        1,
    )
    cfg.write_text(s)
(root / "src/prime_rl/orchestrator/algo/raw.py").write_text('''from __future__ import annotations

from typing import TYPE_CHECKING

import verifiers.v1 as vf

from prime_rl.configs.algorithm import RawRewardAlgoConfig
from prime_rl.orchestrator.algo.base import Algorithm, iter_trainable_traces
from prime_rl.orchestrator.algo.routing import assign_advantages

if TYPE_CHECKING:
    from prime_rl.orchestrator.clients import InferenceClient


class RawRewardAlgorithm(Algorithm):
    """Advantage = scale * reward + shift on every sampled token; no baseline.
    For custom losses (KLPO) that want the terminal reward itself."""

    def __init__(self, config: RawRewardAlgoConfig, clients: InferenceClient):
        super().__init__(config, clients)
        self.scale = config.scale
        self.shift = config.shift

    async def score_episode(self, episode: vf.Episode) -> None:
        for _, trace in iter_trainable_traces([episode]):
            assign_advantages(trace, self.scale * float(trace.reward) + self.shift)
''')
init = root / "src/prime_rl/orchestrator/algo/__init__.py"
s = init.read_text()
if "RawRewardAlgorithm" not in s:
    s = s.replace(
        "from prime_rl.orchestrator.algo.rae import RAEAlgorithm\n",
        "from prime_rl.orchestrator.algo.rae import RAEAlgorithm\nfrom prime_rl.orchestrator.algo.raw import RawRewardAlgorithm\n",
        1,
    )
    s = s.replace('    "debug": DebugAlgorithm,\n}', '    "debug": DebugAlgorithm,\n    "raw": RawRewardAlgorithm,\n}', 1)
    init.write_text(s)
print("patched", root)

ulysses = pathlib.Path(__file__).with_name("prime-rl-ulysses-cp.patch")
if subprocess.run(["git", "apply", "--reverse", "--check", str(ulysses)], cwd=root, capture_output=True).returncode == 0:
    print("ulysses cp patch already applied")
else:
    subprocess.run(["git", "apply", str(ulysses)], cwd=root, check=True)
    print("ulysses cp patch applied")
