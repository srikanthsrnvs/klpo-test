import torch
from prime_rl.configs.algorithm import RawRewardAlgoConfig
from prime_rl.orchestrator.algo import ALGORITHM_CLASSES
print("algo:", ALGORITHM_CLASSES["raw"].__name__, RawRewardAlgoConfig())
from prime_rl.trainer.rl.loss import LossInputs
import klpo_prime
n = 12
mask = torch.tensor([False]*4 + [True]*8)
lp = -torch.rand(n) * 3; lq = lp + 0.1 * torch.randn(n)
lp.requires_grad_(True)
out = klpo_prime.klpo_binary_loss(LossInputs(lp, lq, None, torch.full((n,), 0.7), mask), beta=0.1)
out.loss.backward()
print("loss:", float(out.loss), {k: round(float(v), 4) for k, v in out.metrics.items()})
print("grad only on masked tokens:", bool((lp.grad[~mask] == 0).all()), bool((lp.grad[mask] != 0).all()))
