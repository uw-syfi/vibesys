"""Fake candidate for the CPU check's tests: bugs that only the batched path's workload reaches.

Copied into a temporary candidate root as `engine/server.py`. Each defect below
is one r13 continuous-batching candidates hit on the GPU after passing the
sequential CPU check, reduced to the request shape that triggers it, so the
tests do not depend on request timing. `FAKE_SCHEDULER_BUG` selects one:

- `score_remainder`: chunked teacher-forced scoring gathers a full 512-row index
  for the last, shorter chunk (r13: `[512, 1]` against `[347, 248320]`).
- `inference_mode`: the weights require grad, and after a prompt longer than
  one 2048-token prefill chunk the scheduler projects the last hidden state
  outside `torch.inference_mode`, so autograd meets a tensor made inside it
  (r13, 1 s into accuracy: "Inference tensors cannot be saved for backward").
- `slot_leak`: admission takes one of 16 decode slots per request and never
  returns it, so the 17th request fails at admission.
"""

from __future__ import annotations

import argparse
import os
from typing import TYPE_CHECKING

import reference.server as base
import torch
import uvicorn
from reference.engine import Engine, SamplingParams, StepOutput, TokenLogprobs

if TYPE_CHECKING:
    from collections.abc import Iterator

BUG = os.environ.get("FAKE_SCHEDULER_BUG", "")
if BUG not in ("score_remainder", "inference_mode", "slot_leak"):
    raise SystemExit(f"unknown FAKE_SCHEDULER_BUG {BUG!r}")
PREFILL_CHUNK = 2048
SLOTS = 16


class SchedulerEngine(Engine):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.free_slots = list(range(SLOTS))
        if BUG == "inference_mode":
            self.model.requires_grad_(True)

    def _prefill(self, token_ids: list[int], capacity: int):
        if BUG != "inference_mode" or len(token_ids) <= PREFILL_CHUNK:
            return super()._prefill(token_ids, capacity)
        state, hidden = super()._prefill(token_ids, capacity)
        with torch.inference_mode(False):
            self.model.logits(hidden[0, -1])
        return state, hidden

    def generate(self, prompt_ids: list[int], params: SamplingParams) -> Iterator[StepOutput]:
        if BUG == "slot_leak":
            if not self.free_slots:
                raise RuntimeError(f"admission: no free decode slot ({SLOTS} in use, cap {SLOTS})")
            self.free_slots.pop()
        return super().generate(prompt_ids, params)

    @torch.inference_mode()
    def score(self, token_ids: list[int], top_k: int = 1, chunk: int = 512) -> list[TokenLogprobs]:
        if BUG != "score_remainder":
            return super().score(token_ids, top_k, chunk)
        _, hidden = self._prefill(token_ids, len(token_ids))
        targets = torch.tensor(token_ids[1:], device=self.device)
        out: list[TokenLogprobs] = []
        for s in range(0, len(targets), chunk):
            tgt = targets[s : s + chunk]
            lps = torch.log_softmax(self.model.logits(hidden[0, s : s + len(tgt)]), dim=-1)
            index = targets.new_zeros(chunk, 1)  # the bug: a full-chunk index for every chunk
            index[: len(tgt), 0] = tgt
            given = lps.gather(1, index)[:, 0].tolist()
            top_vals, top_ids = lps.topk(max(top_k, 1), dim=-1)
            for j, lp in enumerate(given):
                top = list(
                    zip(top_ids[j, :top_k].tolist(), top_vals[j, :top_k].tolist(), strict=True)
                )
                out.append(TokenLogprobs(logprob=lp, top=top))
        return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--served-model-name", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--device", default="cpu")
    args = p.parse_args()
    engine = SchedulerEngine(args.model, device=args.device)
    uvicorn.run(base.build_app(engine, args.served_model_name), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
