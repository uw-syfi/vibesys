"""Check Flint numerical parity against the pinned Transformers eager reference.

Public API: ``Completion``, ``ReferenceStep``, ``parse_completion``, and
``assess``. The shell loads a CPU reference and calls the candidate over HTTP;
these public functions validate the observations without I/O or model imports.
"""

from __future__ import annotations

import argparse
import math
import secrets
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import httpx

if TYPE_CHECKING:
    from collections.abc import Sequence

GENERATED_TOKENS = 8
PROMPT_LENGTHS = (1, 16, 32, 512, 513, 514, 1024)
GREEDY_MARGIN = 0.05
MAX_LOGPROB_ERROR = 0.15
MEAN_LOGPROB_ERROR = 0.05


@dataclass(frozen=True)
class Completion:
    """Validated generated IDs and the server's per-token log probabilities."""

    ids: tuple[int, ...]
    logprobs: tuple[float, ...]


@dataclass(frozen=True)
class ReferenceStep:
    """Reference log probabilities for the chosen token and the best token."""

    chosen: float
    best: float


def parse_completion(
    payload: object,
    prompt_tokens: int,
    vocab_size: int,
    *,
    endpoint: Literal["completions", "chat"] = "completions",
) -> Completion:
    """Validate the OpenAI completion and required token-ID extension.

    OpenAI responses permit additional fields; only the consumed fields are
    projected here. Token counts, numeric types and probability values are strict.
    """
    if not isinstance(payload, dict):
        message = "completion response must be a JSON object"
        raise ValueError(message)
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        message = "choices must contain exactly one completion"
        raise ValueError(message)
    choice = choices[0]
    ids = choice.get("token_ids")
    if not isinstance(ids, list) or len(ids) != GENERATED_TOKENS:
        message = f"choices[0].token_ids must contain {GENERATED_TOKENS} tokens"
        raise ValueError(message)
    if any(type(token) is not int or not 0 <= token < vocab_size for token in ids):
        message = "choices[0].token_ids contains a non-integer or out-of-range token"
        raise ValueError(message)
    record = choice.get("logprobs")
    values = None
    if isinstance(record, dict):
        if endpoint == "completions":
            values = record.get("token_logprobs")
        elif isinstance(record.get("content"), list):
            values = [
                row.get("logprob") if isinstance(row, dict) else None for row in record["content"]
            ]
    if not isinstance(values, list) or len(values) != GENERATED_TOKENS:
        message = "choices[0].logprobs.token_logprobs has the wrong length"
        raise ValueError(message)
    if any(
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value > 0
        for value in values
    ):
        message = "choices[0].logprobs.token_logprobs must be finite nonpositive numbers"
        raise ValueError(message)
    usage = payload.get("usage")
    if not isinstance(usage, dict) or any(
        type(usage.get(name)) is not int or usage[name] != expected
        for name, expected in (
            ("prompt_tokens", prompt_tokens),
            ("completion_tokens", GENERATED_TOKENS),
        )
    ):
        message = "usage.prompt_tokens or usage.completion_tokens is incorrect"
        raise ValueError(message)
    return Completion(tuple(ids), tuple(float(value) for value in values))


def assess(completion: Completion, reference: Sequence[ReferenceStep]) -> None:
    """Reject decisive greedy mistakes or excessive log-probability drift."""
    if len(reference) != GENERATED_TOKENS or len(completion.logprobs) != GENERATED_TOKENS:
        message = "reference and completion must contain the full continuation"
        raise ValueError(message)
    errors = []
    for index, (actual, expected) in enumerate(zip(completion.logprobs, reference, strict=True)):
        if not all(
            math.isfinite(value) and value <= 0
            for value in (expected.chosen, expected.best, actual)
        ):
            message = f"nonfinite or positive log probability at continuation token {index}"
            raise ValueError(message)
        if expected.chosen > expected.best:
            message = f"reference best log probability is inconsistent at token {index}"
            raise ValueError(message)
        if expected.best - expected.chosen > GREEDY_MARGIN:
            message = f"decisive greedy mismatch at continuation token {index}"
            raise ValueError(message)
        errors.append(abs(actual - expected.chosen))
    if max(errors) > MAX_LOGPROB_ERROR or sum(errors) / len(errors) > MEAN_LOGPROB_ERROR:
        message = "candidate log probabilities exceed the reference error budget"
        raise ValueError(message)


def _reference_steps(
    model: Any, prompt: list[int], continuation: tuple[int, ...]
) -> list[ReferenceStep]:
    import torch

    # Score the candidate's own continuation to avoid divergence cascades at
    # near ties. Eager attention includes learned sinks on CPU and ROCm alike.
    with torch.inference_mode():
        inputs = torch.tensor([prompt + list(continuation[:-1])], device="cpu")
        logits = (
            model(input_ids=inputs, use_cache=False, logits_to_keep=GENERATED_TOKENS)
            .logits[0]
            .float()
        )
        probabilities = torch.log_softmax(logits, dim=-1)
        chosen = probabilities.gather(1, torch.tensor(continuation)[:, None])[:, 0].tolist()
        best = probabilities.max(dim=-1).values.tolist()
    return [ReferenceStep(given, top) for given, top in zip(chosen, best, strict=True)]


def _run(args: argparse.Namespace) -> None:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        args.model, revision=args.revision, trust_remote_code=False
    )
    reference = AutoModelForCausalLM.from_pretrained(
        args.model,
        revision=args.revision,
        dtype=torch.bfloat16,
        device_map="cpu",
        attn_implementation="eager",
        trust_remote_code=False,
    ).eval()
    if reference.config.model_type != "flint":
        message = "the reference checkpoint must have model_type=flint"
        raise ValueError(message)
    nonce = secrets.token_hex(16)
    # Vary content per invocation; lengths straddle Flint's sliding-window boundary.
    pool = tokenizer.encode(
        f"# case {nonce}\ndef merge(items):\n    return sorted(set(items))\n" * 512
    )
    base = args.url.rstrip("/").removesuffix("/v1")
    with httpx.Client(timeout=args.request_timeout_seconds) as client:
        for length in PROMPT_LENGTHS:
            prompt = pool[:length]
            body = {
                "model": args.model,
                "prompt": prompt,
                "max_tokens": GENERATED_TOKENS,
                "temperature": 0,
                "ignore_eos": True,
                "return_token_ids": True,
                "logprobs": 1,
            }
            response = client.post(f"{base}/v1/completions", json=body)
            response.raise_for_status()
            actual = parse_completion(response.json(), len(prompt), reference.config.vocab_size)
            assess(actual, _reference_steps(reference, prompt, actual.ids))
            repeated = client.post(f"{base}/v1/completions", json=body)
            repeated.raise_for_status()
            again = parse_completion(repeated.json(), len(prompt), reference.config.vocab_size)
            if actual.ids != again.ids:
                message = f"greedy decoding is not deterministic at prompt length {length}"
                raise ValueError(message)
            sys.stdout.write(f"PASS: prompt={length}, continuation={GENERATED_TOKENS}\n")

        messages = [
            {"role": "system", "content": "You are a coding assistant."},
            {"role": "user", "content": "Return a Python expression that reverses xs."},
        ]
        prompt = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
        body = {
            "model": args.model,
            "max_tokens": GENERATED_TOKENS,
            "temperature": 0,
            "ignore_eos": True,
            "return_token_ids": True,
        }
        response = client.post(
            f"{base}/v1/completions", json={**body, "prompt": prompt, "logprobs": 1}
        )
        response.raise_for_status()
        raw = parse_completion(response.json(), len(prompt), reference.config.vocab_size)
        assess(raw, _reference_steps(reference, prompt, raw.ids))
        response = client.post(
            f"{base}/v1/chat/completions", json={**body, "messages": messages, "logprobs": True}
        )
        response.raise_for_status()
        chat = parse_completion(
            response.json(), len(prompt), reference.config.vocab_size, endpoint="chat"
        )
        if chat.ids != raw.ids:
            message = "chat token IDs differ from the pinned chat-template completion"
            raise ValueError(message)
        assess(chat, _reference_steps(reference, prompt, chat.ids))
        sys.stdout.write("PASS: chat template and raw completion agree\n")


def main() -> int:
    """Check the live candidate; no numerical or transport failure is accepted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--request-timeout-seconds", type=float, default=120)
    args = parser.parse_args()
    if len(args.revision) != 40 or any(c not in "0123456789abcdef" for c in args.revision):
        parser.error("--revision must be a full lowercase commit SHA")
    if not math.isfinite(args.request_timeout_seconds) or args.request_timeout_seconds <= 0:
        parser.error("--request-timeout-seconds must be positive and finite")
    try:
        _run(args)
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
        sys.stderr.write(f"Flint accuracy failed: {exc}\n")
        return 1
    else:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
