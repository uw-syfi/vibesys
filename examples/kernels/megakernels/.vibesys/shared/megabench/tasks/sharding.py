"""Tensor-parallel layouts that run a single-device case across ranks.

Trainium2 exposes one chip as four logical NeuronCores of 24 GiB each, so a
P0 case that uses the whole chip runs as four ranks. The single-device
reference stays the specification. The harness generates its inputs, gives
each rank a contiguous shard, and reassembles the ranks' outputs before
grading them exactly as a single-device result.

Megatron layout over stacked per-layer weights:

- ``wq``/``wk``/``wv``/``wg``/``wu`` and their per-row scales split their
  output rows (dim 1 of ``[layer, out, in]``), so each rank owns whole query
  and KV heads (GQA groups stay rank-local) and an MLP slice.
- ``wo``/``wd`` split their input columns (the last dim). W4 weights pack two
  adjacent columns per byte, so packed shards are column shards. Their
  per-output-row scales are replicated.
- MoE experts split along the expert dim: ``wg``/``wu``/``wd`` of
  ``[layer, expert, ...]``. The router is replicated.
- KV caches split their KV-head dim. ``lm_head`` splits the vocabulary.
  ``embed`` is replicated; tied heads read their own vocabulary rows of it.
- Everything else (tokens, norms) is replicated.

Outputs: ``logits`` are vocabulary shards (last dim), the KV writes are
KV-head shards, and every other output is replicated and must agree on all
ranks.
"""

from __future__ import annotations

import torch

from ..cases import Case

TP_FAMILIES = ("dense_step", "moe_step", "quant_step", "spec_target_step")
COLUMN = ("wq", "wk", "wv", "wg", "wu")
ROW = ("wo", "wd")
EXPERT = ("wg", "wu", "wd")


def input_dim(case: Case, name: str) -> int | None:
    """The dim along which ``name`` is split, or None if it is replicated."""
    if case.family == "moe_step" and name in EXPERT:
        return 1
    if name in COLUMN or name in tuple(f"{n}_scale" for n in COLUMN):
        return 1
    if name in ROW:
        return -1
    if name in ("kcache", "vcache"):
        return 2
    if name == "lm_head":
        return 0
    return None


def output_dim(name: str, value: torch.Tensor) -> int | None:
    """The dim along which output ``name`` is split, or None if replicated."""
    if name == "logits":
        return -1
    if name in ("k_write", "v_write"):
        return value.dim() - 2
    return None


def check_divisible(case: Case, values: dict[str, torch.Tensor], world: int) -> None:
    if world == 1:  # one rank holds every input whole: any family runs unsharded
        return
    if case.family not in TP_FAMILIES:
        raise ValueError(f"{case.family} has no tensor-parallel layout")
    p = case.params
    for key in ("q_heads", "kv_heads", "intermediate", "vocab"):
        if p[key] % world:
            raise ValueError(f"{case.id}: {key}={p[key]} does not split {world} ways")
    if case.family == "moe_step" and p["experts"] % world:
        raise ValueError(f"{case.id}: experts={p['experts']} does not split {world} ways")
    for name, value in values.items():
        dim = input_dim(case, name)
        if dim is not None and value.shape[dim] % world:
            raise ValueError(f"{case.id}: input {name} {tuple(value.shape)} does not split along dim {dim}")


def shard(case: Case, values: dict[str, torch.Tensor], rank: int, world: int) -> dict[str, torch.Tensor]:
    """Rank ``rank``'s views of ``values``: contiguous chunks or the replicated tensor."""
    if world == 1:
        return dict(values)
    out = {}
    for name, value in values.items():
        dim = input_dim(case, name)
        out[name] = value if dim is None else value.chunk(world, dim)[rank]
    return out


def shard_outputs(outputs: dict[str, torch.Tensor], rank: int, world: int) -> dict[str, torch.Tensor]:
    """Rank ``rank``'s part of full outputs (the layout a submission returns)."""
    out = {}
    for name, value in outputs.items():
        dim = output_dim(name, value)
        out[name] = value if dim is None else value.chunk(world, dim)[rank]
    return out


def assemble(per_rank: list[dict[str, torch.Tensor]]) -> dict[str, torch.Tensor]:
    """Reassemble rank outputs into full outputs; replicated outputs must agree."""
    names = set(per_rank[0])
    for rank, outputs in enumerate(per_rank):
        if set(outputs) != names:
            raise AssertionError(f"output keys: rank {rank} returned {sorted(outputs)}, rank 0 {sorted(names)}")
    full = {}
    for name in per_rank[0]:
        parts = [outputs[name] for outputs in per_rank]
        if not all(isinstance(part, torch.Tensor) for part in parts):
            raise AssertionError(f"{name}: output must be a Tensor on every rank")
        dim = output_dim(name, parts[0])
        if dim is None:
            for rank, part in enumerate(parts[1:], 1):
                if (part.shape != parts[0].shape or part.dtype != parts[0].dtype
                        or not torch.equal(part, parts[0])):
                    raise AssertionError(f"{name}: replicated output differs between rank 0 and rank {rank}")
            full[name] = parts[0]
        else:
            if any(part.dim() != parts[0].dim() or part.dtype != parts[0].dtype for part in parts):
                raise AssertionError(f"{name}: rank shards differ in rank or dtype")
            full[name] = torch.cat(parts, dim)
    return full
