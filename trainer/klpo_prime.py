"""KLPO token regression (binary KL estimator) as a prime-rl custom loss.

Reference: yifanzhang-pro/KLPO, docs/algorithms.md, "KLPO token regression + Binary KL":

    ell   = log p(a) - log q(a)                 # trainer vs sampler, sampled action
    h     = R - beta * ell                      # per-token return coefficient (detached)
    omega = (1 - q(a)) / (1 - p(a))             # binary-KL score correction (detached)
    loss  = - sum_tokens( sg(h) * sg(omega) * log p(a) )   summed over the trajectory's policy tokens

prime-rl supplies, per sequence: trainer_logprobs (with grad), inference_logprobs (sampler, fixed),
advantages (we arrange for these to be the raw terminal reward R broadcast over the sequence's tokens; see
trainer/prime-rl.patch), loss_mask (policy tokens only: prompt and tool-output tokens excluded).
prime-rl normalizes the sum over the global rl-token count, which is a constant scale per step relative to
KLPO's mean-over-responses; both weight every policy token equally.

Wire-up (TOML):
    [trainer.loss]
    type = "custom"
    import_path = "klpo_prime.klpo_binary_loss"
    [trainer.loss.kwargs]
    beta = 0.1
"""

import math

import torch
from prime_rl.trainer.rl.loss import LossInputs, LossOutputs


def _log1mexp(x: torch.Tensor) -> torch.Tensor:
    """log(1 - exp(x)) for x < 0 without catastrophic cancellation (same form as the KLPO reference)."""
    return torch.where(x < -math.log(2), torch.log1p(-x.exp()), torch.log(-torch.expm1(x)))


def klpo_binary_loss(inputs: LossInputs, beta: float = 0.1, clamp_logp: float = -1e-6) -> LossOutputs:
    mask = inputs.loss_mask
    log_p = inputs.trainer_logprobs[mask]
    log_q = inputs.inference_logprobs[mask]
    rewards = inputs.advantages[mask]  # raw terminal reward R, broadcast per token by the algorithm

    # Binary KL needs strictly negative logps (0 < p, q < 1). A logp of exactly 0 (p = 1) has no complement;
    # nudge it off the boundary rather than producing inf. This only affects near-deterministic tokens.
    log_p_safe = log_p.clamp(max=clamp_logp)
    log_q_safe = log_q.clamp(max=clamp_logp)

    with torch.no_grad():
        ell = log_p_safe - log_q_safe
        h = rewards - beta * ell
        # omega = (1 - q) / (1 - p) = exp(log(1-q) - log(1-p))
        omega = torch.exp(_log1mexp(log_q_safe) - _log1mexp(log_p_safe))
        # binary KL(q || p) for logging only
        q = log_q_safe.exp()
        binary_kl = -q * ell + (1 - q) * (_log1mexp(log_q_safe) - _log1mexp(log_p_safe))

    per_token = -(h * omega) * log_p_safe
    if inputs.loss_weights is not None:
        per_token = per_token * inputs.loss_weights[mask]
    loss = per_token.sum()

    n = max(mask.sum().item(), 1)
    metrics = {
        "klpo/return_coef_mean": h.sum() / n,
        "klpo/omega_mean": omega.sum() / n,
        "klpo/binary_kl_sum": binary_kl.sum(),
        "klpo/log_ratio_mean": ell.sum() / n,
        "klpo/reward": rewards.sum() / n,
    }
    return LossOutputs(loss=loss, metrics=metrics)
