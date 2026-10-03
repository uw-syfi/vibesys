"""Fake candidate for the CPU check's tests: the reference server plus a prefix cache.

Copied into a temporary candidate root as `engine/server.py`. `FAKE_CACHE_BUG`
selects a known defect, or none:

- `none`: a correct cache. A hit copies the cached state into a new state sized
  for this request, then prefills only the uncached suffix.
- `stale_capacity`: a hit resumes the cached state as-is, so the state keeps the
  capacity of the request that created it and a longer chained round
  overflows it.
- `double_length`: the decode loop advances `state.length` a second time after
  the model already did (r9's h1 candidate), so a decode overflows the state.
"""

from __future__ import annotations

import argparse
import os
from typing import TYPE_CHECKING

import reference.server as base
import torch
import uvicorn
from reference.engine import Engine, SamplingParams, StepOutput
from reference.model import AttentionCache, SequenceState, new_sequence_state

if TYPE_CHECKING:
    from collections.abc import Iterator

BUG = os.environ.get("FAKE_CACHE_BUG", "none")
if BUG not in ("none", "stale_capacity", "double_length"):
    raise SystemExit(f"unknown FAKE_CACHE_BUG {BUG!r}")


def _clone(state: SequenceState) -> SequenceState:
    layers = [
        AttentionCache(c.k.clone(), c.v.clone())
        if isinstance(c, AttentionCache)
        else type(c)(c.conv.clone(), c.recurrent.clone())
        for c in state.layers
    ]
    return SequenceState(layers, state.capacity, state.length)


class CachingEngine(Engine):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cache: dict[tuple[int, ...], SequenceState] = {}  # absorbed tokens -> state
        self.last_cached_tokens = 0

    def _resume(self, prompt: list[int], capacity: int) -> tuple[SequenceState, int]:
        hits = [k for k in self.cache if len(k) < len(prompt) and tuple(prompt[: len(k)]) == k]
        if not hits:
            return new_sequence_state(self.cfg, 1, capacity, self.device, self.dtype), 0
        cached = self.cache[max(hits, key=len)]
        if BUG == "stale_capacity":
            return _clone(cached), cached.length
        state = new_sequence_state(self.cfg, 1, capacity, self.device, self.dtype)
        n = cached.length
        for dst, src in zip(state.layers, cached.layers, strict=True):
            if isinstance(dst, AttentionCache):
                dst.k[:, :, :n] = src.k[:, :, :n]
                dst.v[:, :, :n] = src.v[:, :, :n]
            else:
                dst.conv.copy_(src.conv)
                dst.recurrent.copy_(src.recurrent)
        state.length = n
        return state, n

    @torch.inference_mode()
    def generate(self, prompt_ids: list[int], params: SamplingParams) -> Iterator[StepOutput]:
        self._check_len(len(prompt_ids) + params.max_tokens)
        state, hit = self._resume(prompt_ids, len(prompt_ids) + params.max_tokens)
        self.last_cached_tokens = hit
        hidden = self.model(torch.tensor([prompt_ids[hit:]], device=self.device), state)
        absorbed = list(prompt_ids)
        for i in range(params.max_tokens):
            token = int(self.model.logits(hidden[0, -1]).argmax())
            last = i == params.max_tokens - 1
            yield StepOutput(token, None, "length" if last else None)
            if last:
                break
            hidden = self.model(torch.tensor([[token]], device=self.device), state)
            if BUG == "double_length":
                state.length += 1
            absorbed.append(token)
        self.cache[tuple(absorbed)] = _clone(state)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--served-model-name", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--device", default="cpu")
    args = p.parse_args()
    engine = CachingEngine(args.model, device=args.device)

    def usage(prompt_tokens: int, completion_tokens: int) -> dict:
        out = reference_usage(prompt_tokens, completion_tokens)
        out["prompt_tokens_details"] = {"cached_tokens": engine.last_cached_tokens}
        return out

    # The reference handlers report usage through this module function; a candidate
    # built on them swaps in its own. Requests run one at a time on the reference's
    # FIFO worker, so `last_cached_tokens` belongs to the request being answered.
    reference_usage = base._usage
    base._usage = usage
    uvicorn.run(base.build_app(engine, args.served_model_name), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
