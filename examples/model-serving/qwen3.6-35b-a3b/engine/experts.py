"""The MoE experts implementation the baseline registers for ROCm.

Transformers 5.22 defaults to ``grouped_mm`` (``torch._grouped_mm``), which the
torch 2.9.1+rocm6.4 build cannot run, and its ``eager`` fallback synchronizes
with the host once per selected expert. This implementation uses only ordinary
matmuls:

- a few tokens (decode steps): Transformers' own ``batched_mm``, which gathers
  each token's selected expert weights and runs one batched matmul, with no
  host synchronization;
- many tokens (prefill chunks), where gathering weights per token would need
  gigabytes: tokens sorted by expert, one host synchronization for the group
  sizes, then one matmul pair per expert that received tokens.

``register`` adds it to Transformers' experts registry under ``NAME``.
"""

from __future__ import annotations

import torch
from torch.nn import functional as F
from transformers.integrations.moe import ExpertsInterface, batched_mm_experts_forward

NAME = "baseline_matmul"
# Up to this many tokens per call, gather per-token weights (decode); above it, loop over experts.
BATCHED_TOKENS = 16


def _sorted_experts_forward(
    self: torch.nn.Module,
    hidden_states: torch.Tensor,
    top_k_index: torch.Tensor,
    top_k_weights: torch.Tensor,
) -> torch.Tensor:
    num_tokens, top_k = top_k_index.shape
    expert_ids = top_k_index.reshape(-1)
    order = torch.argsort(expert_ids, stable=True)
    counts = torch.bincount(expert_ids, minlength=self.num_experts).tolist()  # the one host sync
    inputs = hidden_states[order // top_k]
    outputs = torch.empty_like(inputs)
    start = 0
    for expert, count in enumerate(counts):
        if count == 0:
            continue
        rows = slice(start, start + count)
        gate, up = F.linear(inputs[rows], self.gate_up_proj[expert]).chunk(2, dim=-1)
        outputs[rows] = F.linear(self.act_fn(gate) * up, self.down_proj[expert])
        start += count
    weighted = outputs.float() * top_k_weights.reshape(-1)[order, None].float()
    unsorted = torch.empty_like(weighted)
    unsorted[order] = weighted
    return unsorted.view(num_tokens, top_k, -1).sum(dim=1).to(hidden_states.dtype)


def experts_forward(
    self: torch.nn.Module,
    hidden_states: torch.Tensor,
    top_k_index: torch.Tensor,
    top_k_weights: torch.Tensor,
) -> torch.Tensor:
    """Batched gather for decode-sized inputs, a per-expert loop for prefill-sized ones."""
    if hidden_states.shape[0] <= BATCHED_TOKENS:
        return batched_mm_experts_forward(self, hidden_states, top_k_index, top_k_weights)
    return _sorted_experts_forward(self, hidden_states, top_k_index, top_k_weights)


def register() -> str:
    """Make ``NAME`` a valid ``experts_implementation``; return it."""
    ExpertsInterface.register(NAME, experts_forward)
    return NAME
