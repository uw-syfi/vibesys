"""Rank mapping and process groups shared by distributed model references."""

from __future__ import annotations

from dataclasses import dataclass

import torch
import torch.distributed as dist

from ..cases import Case


@dataclass(frozen=True)
class ParallelContext:
    rank: int
    tp_rank: int
    ep_rank: int
    tp_group: object
    ep_group: object


_CONTEXT: tuple[object, int, int, int, ParallelContext] | None = None


def initialize(case: Case) -> ParallelContext:
    """Every rank creates groups in the same order: rank = ep_rank*tp+tp_rank."""
    global _CONTEXT
    if not dist.is_initialized() or dist.get_world_size() != case.gpus:
        raise ValueError(f"{case.id} requires an initialized {case.gpus}-rank process group")
    if case.gpus != case.tp * case.ep:
        raise ValueError("GPU count must equal TP times EP")
    rank = dist.get_rank()
    world_group = dist.group.WORLD
    if _CONTEXT and _CONTEXT[:4] == (world_group, case.tp, case.ep, rank):
        return _CONTEXT[4]
    tp_group, ep_group = None, None
    for expert_rank in range(case.ep):
        ranks = [expert_rank * case.tp + tensor_rank for tensor_rank in range(case.tp)]
        group = dist.new_group(ranks)
        if rank in ranks:
            tp_group = group
    for tensor_rank in range(case.tp):
        ranks = [expert_rank * case.tp + tensor_rank for expert_rank in range(case.ep)]
        group = dist.new_group(ranks)
        if rank in ranks:
            ep_group = group
    context = ParallelContext(rank, rank % case.tp, rank // case.tp, tp_group, ep_group)
    _CONTEXT = (world_group, case.tp, case.ep, rank, context)
    return context


def exchange(value, send_counts, receive_counts, group):
    """``all_to_all_single`` with host-side split sizes along dim 0."""
    output = value.new_empty((sum(receive_counts), *value.shape[1:]))
    dist.all_to_all_single(output, value.contiguous(), receive_counts, send_counts, group=group)
    return output


def exchange_counts(owner, group):
    """Host-side send/receive row counts for rows destined to ``owner`` ranks.

    Callers order rows stably by owner first, so every destination receives
    the source's rows in their original order.
    """
    send = torch.bincount(owner, minlength=dist.get_world_size(group))
    receive = torch.empty_like(send)
    dist.all_to_all_single(receive, send, group=group)
    return send.tolist(), receive.tolist()
