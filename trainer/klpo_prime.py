"""KLPO token regression (binary KL estimator) as a prime-rl custom loss.

Reference: yifanzhang-pro/KLPO, docs/algorithms.md, "KLPO token regression + Binary KL":

    ell   = log p(a) - log q(a)                 # trainer vs sampler, sampled action
    h     = R - beta * ell                      # per-token return coefficient (detached)
    omega = (1 - q(a)) / (1 - p(a))             # binary-KL score correction (detached)
    loss  = - sum_tokens( sg(h) * sg(omega) * log p(a) )   summed over the trajectory's policy tokens

prime-rl supplies, per sequence: trainer_logprobs (with grad), inference_logprobs (sampler, fixed), advantages
(the per-token credit the orchestrator's algorithm assigned: the group-centered reward under `grpo`), loss_mask
(policy tokens only: prompt and tool-output tokens excluded). prime-rl normalizes the sum over the global
rl-token count (KLPO's reference averages over responses); both weight every policy token equally.

Departures from the reference, all configurable:
* omega is clipped to [omega_min, omega_max]. The reference leaves it unbounded; on near-deterministic tokens
  (tool-call scaffolding) 1 - p is tiny, and a small trainer/sampler gap turns omega into a 10x to 1000x weight.
* R = advantage * adv_scale. The per-token regression target is ell = R / beta, so keep |R| / beta well under one
  nat: with |R| <= ~0.75 and beta = 2 the target is <= 0.4 nat. At beta = 0.1 the target (7.5 nats) is
  unreachable, h never reaches zero on positive-advantage tokens, and the loss degenerates to unclipped REINFORCE.
* log-probs are clamped at clamp_logp (p <= 1 - 1e-4) so 1 - p cannot collapse to the 1e-6 an earlier clamp allowed.

Wire-up (TOML):
    [trainer.loss]
    type = "custom"
    import_path = "klpo_prime.klpo_binary_loss"
    [trainer.loss.kwargs]
    beta = 2.0
    omega_max = 10.0
"""

import math

import torch
from prime_rl.trainer.rl.loss import LossInputs, LossOutputs


def _log1mexp(x: torch.Tensor) -> torch.Tensor:
    """log(1 - exp(x)) for x < 0 without catastrophic cancellation (same form as the KLPO reference)."""
    return torch.where(x < -math.log(2), torch.log1p(-x.exp()), torch.log(-torch.expm1(x)))


def klpo_binary_loss(
    inputs: LossInputs,
    beta: float = 2.0,
    adv_scale: float = 1.0,
    clamp_logp: float = -1e-4,
    omega_min: float = 0.1,
    omega_max: float = 10.0,
) -> LossOutputs:
    mask = inputs.loss_mask
    log_p = inputs.trainer_logprobs[mask]
    log_q = inputs.inference_logprobs[mask]
    rewards = inputs.advantages[mask] * adv_scale

    # Binary KL needs strictly negative logps (0 < p, q < 1). Clamp both away from 0 so 1 - p stays finite.
    log_p_safe = log_p.clamp(max=clamp_logp)
    log_q_safe = log_q.clamp(max=clamp_logp)

    with torch.no_grad():
        ell = log_p_safe - log_q_safe
        h = rewards - beta * ell
        # omega = (1 - q) / (1 - p) = exp(log(1-q) - log(1-p)), then bounded
        omega_raw = torch.exp(_log1mexp(log_q_safe) - _log1mexp(log_p_safe))
        omega = omega_raw.clamp(omega_min, omega_max)
        clipped = (omega_raw < omega_min) | (omega_raw > omega_max)
        # binary KL(q || p) for logging only
        q = log_q_safe.exp()
        binary_kl = -q * ell + (1 - q) * (_log1mexp(log_q_safe) - _log1mexp(log_p_safe))

    per_token = -(h * omega) * log_p_safe
    if inputs.loss_weights is not None:
        per_token = per_token * inputs.loss_weights[mask]
    loss = per_token.sum()

    n = max(mask.sum().item(), 1)
    zero = log_p.new_zeros(())

    def _max(t: torch.Tensor) -> torch.Tensor:
        return t.max() if t.numel() else zero

    metrics = {
        "klpo/return_coef_mean": h.sum() / n,
        "klpo/omega_mean": omega.sum() / n,
        "klpo/omega_raw_max": _max(omega_raw),
        "klpo/omega_clipped_frac": clipped.float().sum() / n,
        "klpo/log_ratio_mean": ell.sum() / n,
        "klpo/log_ratio_abs_max": _max(ell.abs()),
        "klpo/binary_kl_sum": binary_kl.sum(),
        "klpo/reward": rewards.sum() / n,
    }
    return LossOutputs(loss=loss, metrics=metrics)
