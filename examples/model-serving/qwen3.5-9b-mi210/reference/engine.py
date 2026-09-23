"""Reference inference engine: one sequence at a time, contiguous per-request state.

Scheduling surface for later optimization rounds:
- `Engine.generate` runs prefill then a decode loop for a single request. A
  continuous-batching scheduler would replace the per-request loop with a step
  loop over many `SequenceState`s (paged KV for attention layers, a slot pool
  for GDN conv/recurrent state).
- `Engine.score` is the teacher-forced path used by the accuracy checker and by
  `echo` + `logprobs` requests.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import torch
from transformers import AutoTokenizer, PreTrainedTokenizerBase

from .config import TextConfig, load_text_config, resolve_model_dir
from .model import Qwen35ForCausalLM, new_sequence_state
from .weights import load_model

log = logging.getLogger(__name__)

MAX_LOGPROBS = 20


@dataclass(frozen=True)
class SamplingParams:
    max_tokens: int
    temperature: float = 0.0  # 0 = greedy
    top_p: float = 1.0
    seed: int | None = None
    ignore_eos: bool = False
    min_tokens: int = 0
    logprobs: int | None = None  # top-k logprobs per generated token

    def __post_init__(self) -> None:
        if self.max_tokens < 0:
            raise ValueError("max_tokens must be >= 0")
        if self.temperature < 0:
            raise ValueError("temperature must be >= 0")
        if not 0 < self.top_p <= 1:
            raise ValueError("top_p must be in (0, 1]")
        if self.logprobs is not None and not 0 <= self.logprobs <= MAX_LOGPROBS:
            raise ValueError(f"logprobs must be in [0, {MAX_LOGPROBS}]")


@dataclass(frozen=True)
class TokenLogprobs:
    """Logprob of the chosen/given token plus the top-k alternatives at one position."""

    logprob: float
    top: list[tuple[int, float]] = field(default_factory=list)


@dataclass(frozen=True)
class StepOutput:
    token_id: int
    logprobs: TokenLogprobs | None
    finish_reason: str | None  # "stop" | "length" on the last step


def _token_logprobs(logprobs_row: torch.Tensor, token_id: int, k: int) -> TokenLogprobs:
    top = []
    if k > 0:
        vals, ids = logprobs_row.topk(k)
        top = list(zip(ids.tolist(), vals.tolist(), strict=True))
    return TokenLogprobs(logprob=float(logprobs_row[token_id]), top=top)


class Engine:
    def __init__(
        self,
        model: str,
        device: str = "cuda",
        dtype: torch.dtype = torch.bfloat16,
        max_model_len: int = 32768,
    ) -> None:
        self.model_dir = resolve_model_dir(model)
        self.device = torch.device(device)
        self.dtype = dtype
        self.max_model_len = max_model_len
        self.cfg: TextConfig = load_text_config(self.model_dir)
        t0 = time.perf_counter()
        self.model: Qwen35ForCausalLM = load_model(self.model_dir, self.cfg, self.device, dtype)
        log.info("loaded weights in %.1fs", time.perf_counter() - t0)
        self.tokenizer: PreTrainedTokenizerBase = AutoTokenizer.from_pretrained(self.model_dir)
        stop = {self.cfg.eos_token_id}
        if self.tokenizer.eos_token_id is not None:
            stop.add(self.tokenizer.eos_token_id)
        self.stop_token_ids = frozenset(stop)

    # ------------------------------------------------------------------ primitives

    def _prefill(self, token_ids: list[int], capacity: int):
        state = new_sequence_state(self.cfg, 1, capacity, self.device, self.dtype)
        ids = torch.tensor([token_ids], device=self.device)
        hidden = self.model(ids, state)
        return state, hidden

    def _check_len(self, total: int) -> None:
        if total > self.max_model_len:
            raise ValueError(
                f"prompt + max_tokens = {total} exceeds max_model_len {self.max_model_len}"
            )

    def _sample(
        self, logits: torch.Tensor, params: SamplingParams, gen: torch.Generator | None
    ) -> int:
        if params.temperature == 0:
            return int(logits.argmax())
        probs = torch.softmax(logits / params.temperature, dim=-1)
        if params.top_p < 1:
            sorted_p, sorted_ids = probs.sort(descending=True)
            keep = sorted_p.cumsum(-1) - sorted_p < params.top_p
            probs = torch.zeros_like(probs).scatter_(-1, sorted_ids[keep], sorted_p[keep])
        return int(torch.multinomial(probs, 1, generator=gen))

    # ------------------------------------------------------------------ public API

    @torch.inference_mode()
    def generate(self, prompt_ids: list[int], params: SamplingParams) -> Iterator[StepOutput]:
        """Yield one `StepOutput` per generated token. Consumes GPU until exhausted."""
        if not prompt_ids:
            raise ValueError("prompt must contain at least one token")
        self._check_len(len(prompt_ids) + params.max_tokens)
        if params.max_tokens == 0:
            return
        gen = None
        if params.temperature > 0 and params.seed is not None:
            gen = torch.Generator(device=self.device).manual_seed(params.seed)
        state, hidden = self._prefill(prompt_ids, len(prompt_ids) + params.max_tokens)
        logits = self.model.logits(hidden[0, -1])
        for i in range(params.max_tokens):
            if i < params.min_tokens:
                logits[list(self.stop_token_ids)] = float("-inf")
            token = self._sample(logits, params, gen)
            lp = None
            if params.logprobs is not None:
                lp = _token_logprobs(torch.log_softmax(logits, dim=-1), token, params.logprobs)
            finish = None
            if not params.ignore_eos and token in self.stop_token_ids:
                finish = "stop"
            elif i == params.max_tokens - 1:
                finish = "length"
            yield StepOutput(token, lp, finish)
            if finish is not None:
                return
            hidden = self.model(torch.tensor([[token]], device=self.device), state)
            logits = self.model.logits(hidden[0, -1])

    @torch.inference_mode()
    def score(self, token_ids: list[int], top_k: int = 1, chunk: int = 512) -> list[TokenLogprobs]:
        """Teacher-forced logprobs: entry i describes the distribution predicting token_ids[i + 1]
        (and the logprob of that given token). Returns len(token_ids) - 1 entries."""
        self._check_len(len(token_ids))
        _, hidden = self._prefill(token_ids, len(token_ids))
        out: list[TokenLogprobs] = []
        targets = token_ids[1:]
        for s in range(0, len(targets), chunk):  # bound the fp32 [T, vocab] logits footprint
            tgt = torch.tensor(targets[s : s + chunk], device=self.device)
            lps = torch.log_softmax(self.model.logits(hidden[0, s : s + len(tgt)]), dim=-1)
            given = lps.gather(1, tgt[:, None])[:, 0].tolist()
            top_vals, top_ids = lps.topk(max(top_k, 1), dim=-1)
            top_vals, top_ids = top_vals[:, :top_k].tolist(), top_ids[:, :top_k].tolist()
            for j in range(len(given)):
                out.append(
                    TokenLogprobs(
                        logprob=given[j], top=list(zip(top_ids[j], top_vals[j], strict=True))
                    )
                )
        return out
