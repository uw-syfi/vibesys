"""Run Qwen3.6-35B-A3B with Transformers: prefill one request at a time, decode them together.

``Generator.load`` loads the BF16 checkpoint sharded over every visible GPU
(``device_map="auto"``), or onto the CPU when there is none. ``start`` prefills
one prompt in fixed chunks and returns its ``Generation``; ``next_step`` picks
that request's next token, and ``decode`` runs one forward pass that feeds the
last token of every running generation at once. Nothing here is thread-safe:
one thread owns a Generator.

The prefix cache keeps deep copies of the model cache (attention KV plus the
Gated DeltaNet convolution and recurrent states). A recurrent state cannot be
cut back to an earlier position, so a request reuses a snapshot only where one
was taken: at a chunk boundary, or after all but the last token of an earlier
prompt. Because a reused snapshot was computed over the same chunk boundaries a
cold prefill would use, reuse does not change the computation, and identical
greedy requests return identical tokens whether or not they hit the cache.
"""

from __future__ import annotations

import copy
import json
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, DynamicCache, GenerationConfig

from engine import batching, experts
from engine.protocol import ChatRequest, FinishReason, RequestError, Sampling

CHECKPOINT = json.loads((Path(__file__).with_name("checkpoint.json")).read_text())


def experts_implementation(hip_version: str | None) -> str | None:
    """The Transformers MoE experts implementation to request; None keeps its default.

    Transformers 5.22 defaults to ``grouped_mm`` (``torch._grouped_mm``). The
    torch 2.9.1+rocm6.4 build raises "grouped gemm is not supported on ROCM"
    when that kernel runs, after Transformers' availability check has passed
    (an MI250 reports compute capability 9.0), so on ROCm every request would
    fail. Transformers' ``eager`` alternative synchronizes with the host once
    per selected expert, so ROCm uses ``engine.experts`` instead.
    """
    return experts.NAME if hip_version else None


@dataclass(frozen=True)
class Step:
    """One generated token. ``emit`` is false for an end-of-sequence token that ends the output."""

    token_id: int
    emit: bool
    finish: FinishReason | None


class PrefixCache:
    """LRU snapshots of the model cache, keyed by the token ids they cover."""

    def __init__(self, chunk_tokens: int, max_tokens: int, max_entries: int) -> None:
        self._chunk = chunk_tokens
        self._max_tokens = max_tokens
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[int, ...], DynamicCache] = OrderedDict()
        self._tokens = 0

    def _usable(self, key: tuple[int, ...], prompt: tuple[int, ...]) -> bool:
        length = len(key)
        return (
            0 < length < len(prompt)
            and (length % self._chunk == 0 or length == len(prompt) - 1)
            and prompt[:length] == key
        )

    def lookup(self, prompt: tuple[int, ...]) -> tuple[int, DynamicCache | None]:
        """The longest usable snapshot for ``prompt``, copied, and how many tokens it covers."""
        best = max((k for k in self._entries if self._usable(k, prompt)), key=len, default=None)
        if best is None:
            return 0, None
        self._entries.move_to_end(best)
        return len(best), copy.deepcopy(self._entries[best])

    def store(self, key: tuple[int, ...], cache: DynamicCache) -> None:
        """Keep a copy of ``cache``, which covers exactly ``key``."""
        if key in self._entries:
            self._entries.move_to_end(key)
            return
        if not key or len(key) > self._max_tokens or self._max_entries == 0:
            return
        self._entries[key] = copy.deepcopy(cache)
        self._tokens += len(key)
        while self._tokens > self._max_tokens or len(self._entries) > self._max_entries:
            evicted, _ = self._entries.popitem(last=False)
            self._tokens -= len(evicted)


class Generation:
    """One request after prefill: its cache, the logits for its next token, and its progress."""

    def __init__(
        self,
        generator: Generator,
        sampling: Sampling,
        cached_tokens: int,
        cache: DynamicCache,
        logits: torch.Tensor,
        max_tokens: int,
    ) -> None:
        self._generator = generator
        self._sampling = sampling
        self.cached_tokens = cached_tokens
        self.cache = cache
        self.logits = logits
        self._max_tokens = max_tokens
        self._count = 0
        self.last_token: int | None = None
        self.done = False
        self._rng = None
        if sampling.seed is not None:
            self._rng = torch.Generator(device=logits.device).manual_seed(sampling.seed)

    def next_step(self) -> Step:
        """Pick the next token from the current logits; ``decode`` must run before the next call."""
        g = self._generator
        token = g.pick(self.logits, self._sampling, self._count, self._rng)
        self._count += 1
        self.last_token = token
        if token in g.eos_ids and not self._sampling.ignore_eos:
            self.done = True
            return Step(token, emit=False, finish="stop")
        finish: FinishReason | None = "length" if self._count == self._max_tokens else None
        self.done = finish is not None
        return Step(token, emit=True, finish=finish)


class Generator:
    """The model, its tokenizer and the prefix cache, owned by one thread."""

    def __init__(
        self,
        model: Any,
        tokenizer: Any,
        eos_ids: frozenset[int],
        *,
        max_model_len: int,
        chunk_tokens: int,
        prefix_cache_tokens: int,
        prefix_cache_entries: int,
    ) -> None:
        self.model = model
        self.tokenizer = tokenizer
        self.eos_ids = eos_ids
        self.max_model_len = max_model_len
        self._chunk = chunk_tokens
        self._prefix_cache = PrefixCache(chunk_tokens, prefix_cache_tokens, prefix_cache_entries)
        self._device = model.get_input_embeddings().weight.device
        # Decode counters, shown on /health: tokens fed per step tell whether requests were batched.
        self.decode_steps = 0
        self.decoded_tokens = 0
        self.largest_batch = 0
        think_end = tokenizer.convert_tokens_to_ids("</think>")
        self.think_end_id = (
            think_end
            if isinstance(think_end, int) and think_end != tokenizer.unk_token_id
            else None
        )

    @classmethod
    def load(
        cls, model: str, revision: str | None, *, experts_impl: str | None = None, **options: int
    ) -> Generator:
        """Load BF16 weights, sharded over all visible GPUs, else on the CPU.

        ``experts_impl`` names the MoE experts implementation; None chooses
        ``experts_implementation(torch.version.hip)``.
        """
        tokenizer = AutoTokenizer.from_pretrained(model, revision=revision)
        device_map = "auto" if torch.cuda.is_available() else None
        experts.register()
        chosen = experts_impl or experts_implementation(torch.version.hip)
        lm = AutoModelForCausalLM.from_pretrained(
            model,
            revision=revision,
            dtype=torch.bfloat16,
            device_map=device_map,
            **({"experts_implementation": chosen} if chosen else {}),
        )
        lm.eval()
        if device_map is not None:
            placed = {str(p.device) for p in lm.parameters()}
            if any(d in ("cpu", "meta", "disk") for d in placed):
                message = f"weights did not fit the visible GPUs (placed on {sorted(placed)}); refusing to offload"
                raise RuntimeError(message)
        try:
            eos = GenerationConfig.from_pretrained(model, revision=revision).eos_token_id
        except OSError:
            eos = None
        if eos is None:
            eos = lm.config.eos_token_id
        eos_ids = frozenset([eos] if isinstance(eos, int) else (eos or []))
        return cls(lm, tokenizer, eos_ids, **options)

    def encode_text(self, prompt: str) -> tuple[int, ...]:
        return tuple(self.tokenizer.encode(prompt))

    def encode_chat(self, request: ChatRequest) -> tuple[int, ...]:
        """Render the checkpoint's chat template; thinking is off unless the request enables it."""
        try:
            text = self.tokenizer.apply_chat_template(
                list(request.messages),
                tools=list(request.tools) if request.tools else None,
                add_generation_prompt=True,
                tokenize=False,
                **{"enable_thinking": False, **request.template_kwargs},
            )
        except Exception as exc:  # jinja2 raises TemplateError subclasses and plain errors alike
            raise RequestError(f"chat template rejected the messages: {exc}") from exc
        return tuple(self.tokenizer.encode(text, add_special_tokens=False))

    def start(self, prompt: tuple[int, ...], sampling: Sampling) -> Generation:
        """Validate lengths, reuse the longest usable snapshot, and prefill the rest of the prompt."""
        if not prompt:
            raise RequestError("prompt must contain at least one token")
        if any(t >= self.model.config.vocab_size for t in prompt):
            raise RequestError("prompt contains a token id outside the vocabulary")
        budget = self.max_model_len - len(prompt)
        if budget < 1 or (sampling.max_tokens is not None and sampling.max_tokens > budget):
            raise RequestError(
                f"prompt ({len(prompt)} tokens) plus max_tokens ({sampling.max_tokens}) "
                f"exceeds the context length {self.max_model_len}"
            )
        with torch.inference_mode():
            reused, cache = self._prefix_cache.lookup(prompt)
            if cache is None:
                cache = DynamicCache(config=self.model.config)
            logits = self.prefill(prompt, reused, cache)
        max_tokens = sampling.max_tokens if sampling.max_tokens is not None else budget
        return Generation(self, sampling, reused, cache, logits, max_tokens)

    def decode(self, generations: list[Generation]) -> None:
        """Feed each generation's last token in one forward pass and store its next logits.

        One request runs alone on its own cache. Several are stacked into one
        batch: left-padded attention keys and values, a padding mask, and each
        request's own position.
        """
        self.decode_steps += 1
        self.decoded_tokens += len(generations)
        self.largest_batch = max(self.largest_batch, len(generations))
        with torch.inference_mode():
            if len(generations) == 1:
                g = generations[0]
                g.logits = self.forward([g.last_token], g.cache)
                return
            merged, lengths = batching.merge([g.cache for g in generations])
            out = self.model(
                input_ids=torch.tensor([[g.last_token] for g in generations], device=self._device),
                attention_mask=batching.padding_mask(lengths, self._device),
                position_ids=torch.tensor([[length] for length in lengths], device=self._device),
                past_key_values=merged,
                use_cache=True,
                logits_to_keep=1,
            )
            logits = out.logits[:, -1].float()
            for g, cache, row in zip(
                generations, batching.split(merged, lengths), logits, strict=True
            ):
                g.cache = cache
                g.logits = row

    def forward(self, ids: list[int] | tuple[int, ...], cache: DynamicCache) -> torch.Tensor:
        """Run ``ids`` through the model on top of ``cache``; logits of the last position."""
        input_ids = torch.tensor([list(ids)], dtype=torch.long, device=self._device)
        out = self.model(
            input_ids=input_ids, past_key_values=cache, use_cache=True, logits_to_keep=1
        )
        return out.logits[0, -1].float()

    def prefill(self, prompt: tuple[int, ...], start: int, cache: DynamicCache) -> torch.Tensor:
        """Fill ``cache`` from ``start`` through the prompt; logits after its last token.

        Chunks end at multiples of the chunk size, so the computation never
        depends on where a reused snapshot began. The last chunk boundary before
        the final token, and the position just before it, are snapshotted.
        """
        body = len(prompt) - 1
        last_boundary = (body // self._chunk) * self._chunk
        position = start
        while position < body:
            end = min((position // self._chunk + 1) * self._chunk, body)
            self.forward(prompt[position:end], cache)
            position = end
            if position == last_boundary and position != body:
                self._prefix_cache.store(prompt[:position], cache)
        if body > start:
            self._prefix_cache.store(prompt[:body], cache)
        return self.forward(prompt[body:], cache)

    def pick(
        self, logits: torch.Tensor, sampling: Sampling, generated: int, rng: torch.Generator | None
    ) -> int:
        """Greedy at temperature 0; otherwise top-k/top-p sampling."""
        if not sampling.ignore_eos and generated < sampling.min_tokens and self.eos_ids:
            logits = logits.clone()
            logits[list(self.eos_ids)] = float("-inf")
        if sampling.temperature == 0.0:
            return int(torch.argmax(logits))
        logits = logits / sampling.temperature
        if sampling.top_k > 0:
            kth = torch.topk(logits, min(sampling.top_k, logits.numel())).values[-1]
            logits = logits.masked_fill(logits < kth, float("-inf"))
        probs = torch.softmax(logits, dim=-1)
        if sampling.top_p < 1.0:
            sorted_probs, order = torch.sort(probs, descending=True)
            outside = torch.cumsum(sorted_probs, dim=-1) - sorted_probs > sampling.top_p
            sorted_probs[outside] = 0.0
            probs = torch.zeros_like(probs).scatter_(0, order, sorted_probs)
        return int(torch.multinomial(probs, 1, generator=rng))
