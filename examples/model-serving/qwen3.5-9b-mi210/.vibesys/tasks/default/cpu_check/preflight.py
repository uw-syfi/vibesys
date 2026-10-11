"""Replay of the benchmark's prefix-cache preflight against the candidate server.

The GPU benchmark (`uw-syfi/request-factory`, which `session_runner` wraps)
refuses to measure a server that does not report a prefix-cache hit. Its probe
is the same prompt sent twice in a row, so the check below sends exactly that:
one prompt of `PROBE_TOKENS` tokens, twice, sequentially, to `/v1/completions`
with `max_tokens` 1 and `temperature` 0, streamed. The hit is
`usage.prompt_tokens_details.cached_tokens` of the final streamed usage chunk
of the second response, which must be greater than 0.

The chained sessions do not cover this: they extend a finished request, while
the probe repeats a prompt whose output was a single token.
"""

from __future__ import annotations

import json
import random

import httpx

# request-factory `src/runner.rs` PREFLIGHT_PROBE_TOKENS at rev 118da61 (the probe is the
# last min(pool, 8192) tokens of the token pool). Rust and Python cannot share the
# definition, so keep the two in step by hand.
PROBE_TOKENS = 8192
CONTRACT = (
    f"the benchmark's prefix-cache preflight sends one {PROBE_TOKENS}-token prompt twice in a "
    "row (streamed, max_tokens 1, temperature 0) and needs "
    "usage.prompt_tokens_details.cached_tokens > 0 on the second response"
)


def probe_prompt(vocab_size: int, seed: int = 1) -> list[int]:
    rng = random.Random(seed)
    return [rng.randrange(1, vocab_size) for _ in range(PROBE_TOKENS)]


def streamed_cached_tokens(
    client: httpx.Client, base_url: str, model: str, prompt: list[int]
) -> int | None:
    """`cached_tokens` from the final usage chunk of one preflight-shaped request.

    None when the usage chunk or its `prompt_tokens_details.cached_tokens` is absent.
    Raises on a non-200 response.
    """
    body = {
        "model": model,
        "prompt": prompt,
        "max_tokens": 1,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    usage = None
    with client.stream("POST", f"{base_url}/v1/completions", json=body) as r:
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code} from server: {r.read().decode()[:500]}")
        for line in r.iter_lines():
            if line.startswith("data: ") and line != "data: [DONE]":
                usage = json.loads(line[len("data: ") :]).get("usage") or usage
    return ((usage or {}).get("prompt_tokens_details") or {}).get("cached_tokens")


def replay(client: httpx.Client, base_url: str, model: str, vocab_size: int) -> str | None:
    """None if the second response reports a hit; otherwise why the preflight would fail."""
    prompt = probe_prompt(vocab_size)
    try:
        streamed_cached_tokens(client, base_url, model, prompt)
        cached = streamed_cached_tokens(client, base_url, model, prompt)
    except Exception as exc:  # any failure is the finding to report
        return f"request failed: {type(exc).__name__}: {exc}; {CONTRACT}"
    if not cached:
        return f"second response reported cached_tokens={cached}; {CONTRACT}"
    return None
