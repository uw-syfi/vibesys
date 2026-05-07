"""
Prefix-caching workload benchmark for an OpenAI-compatible /v1/completions
server.

Workload (per request):
  - 32 768 tokens of shared prefix (identical across every request — the
    same shared seed is used on every invocation, so a long-lived prefix
    cache stays hot).
  - 128 tokens of unique tail.
  - 128 tokens of generation, temperature 0, ignore_eos so we always get the
    full 128 decode tokens.

The 20 requests are dispatched concurrently. The headline metric is the
**aggregate output throughput** (sum of all output tokens / wall clock).

This benchmark synthesises prompts as raw token IDs and sends them via
``prompt: list[int]`` (vLLM-compatible). The server must accept either a
``str`` prompt or a ``list[int]`` prompt; the latter is what's used here so
the 32 k shared portion is byte-identical across requests and across
invocations (which is what makes the prefix cache hit).

Usage:
    python benchmark.py --url http://localhost:8000

Smaller smoke run (judge sanity check):
    python benchmark.py --url http://localhost:8000 --num-requests 2 --max-tokens 64
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import statistics
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import httpx


DEFAULT_MODEL_ID = "allenai/Olmo-Hybrid-7B"


@dataclass
class RequestResult:
    idx: int
    ttft_s: float | None
    total_s: float
    output_tokens: int
    decode_tps: float
    error: str | None


# ---------------------------------------------------------------------------
# Synthetic prompt construction
# ---------------------------------------------------------------------------


def build_token_ids(
    shared_len: int,
    unique_len: int,
    n_requests: int,
    vocab_size: int,
    seed: int,
    shared_seed: int,
) -> tuple[list[int], list[list[int]]]:
    """Build (shared_prefix, [unique_tail_per_request]).

    Two independent RNGs:
      - ``shared_seed`` (default 0): controls the shared prefix. Same seed
        across invocations -> identical 32 k prefix bytes -> deterministic
        prefix-cache hits.
      - ``seed`` (default = wall clock): controls per-request unique tails,
        so consecutive runs don't reuse the same unique tails (which would
        let the previous run's cache contaminate this run's measurement).
    """
    lo, hi = 100, max(101, vocab_size - 100)
    shared_rng = random.Random(shared_seed)
    shared = [shared_rng.randint(lo, hi) for _ in range(shared_len)]
    uniques: list[list[int]] = []
    for r in range(n_requests):
        rr = random.Random(seed + 1 + r)
        uniques.append([rr.randint(lo, hi) for _ in range(unique_len)])
    return shared, uniques


def _load_tokenizer_vocab(model_id: str) -> int:
    """Return the tokenizer vocab size; only used to clamp synthetic IDs."""
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise SystemExit("transformers is required: pip install transformers") from exc
    tok = AutoTokenizer.from_pretrained(model_id)
    return tok.vocab_size


# ---------------------------------------------------------------------------
# Per-request streaming
# ---------------------------------------------------------------------------


async def stream_one_request(
    client: httpx.AsyncClient,
    url: str,
    model_id: str,
    token_ids: list[int],
    max_tokens: int,
    idx: int,
) -> RequestResult:
    """Send a /v1/completions streaming request and measure TTFT + decode."""
    body = {
        "model": model_id,
        "prompt": token_ids,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "ignore_eos": True,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    t_start = time.perf_counter()
    t_first: float | None = None
    output_tokens = 0
    error: str | None = None

    try:
        async with client.stream(
            "POST",
            f"{url}/v1/completions",
            json=body,
            headers={"content-type": "application/json"},
            timeout=httpx.Timeout(connect=30, read=600, write=120, pool=30),
        ) as resp:
            if resp.status_code != 200:
                body_bytes = await resp.aread()
                raise RuntimeError(
                    f"http {resp.status_code}: "
                    f"{body_bytes.decode('utf-8', errors='replace')[:500]}"
                )
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[len("data: "):]
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = event.get("choices") or []
                if choices:
                    text = choices[0].get("text") or ""
                    if text and t_first is None:
                        t_first = time.perf_counter()
                    if text:
                        # Server may not include usage on every chunk; count
                        # chunks as a fallback so single-token chunks are
                        # tracked even before the final usage event arrives.
                        output_tokens += 1
                usage = event.get("usage")
                if usage and usage.get("completion_tokens") is not None:
                    # Authoritative count if the server reports it.
                    output_tokens = usage["completion_tokens"]
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    t_end = time.perf_counter()
    ttft = (t_first - t_start) if t_first is not None else None
    decode_window = max(t_end - (t_first or t_start), 1e-6)
    decode_tps = output_tokens / decode_window if output_tokens else 0.0

    return RequestResult(
        idx=idx,
        ttft_s=ttft,
        total_s=t_end - t_start,
        output_tokens=output_tokens,
        decode_tps=decode_tps,
        error=error,
    )


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = (len(s) - 1) * (p / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] * (hi - k) + s[hi] * (k - lo)


def _pct_block(vals: list[float]) -> dict | None:
    if not vals:
        return None
    s = sorted(vals)
    return {
        "mean": sum(s) / len(s),
        "p50": percentile(s, 50),
        "p90": percentile(s, 90),
        "p95": percentile(s, 95),
        "p99": percentile(s, 99),
        "max": max(s),
        "min": min(s),
    }


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------


async def run_benchmark(args: argparse.Namespace) -> dict:
    n_requests = args.num_requests if args.num_requests is not None else args.requests
    if n_requests <= 0:
        raise SystemExit("--num-requests must be > 0")

    print(f"[bench] loading tokenizer for vocab size: {args.model}", file=sys.stderr)
    vocab_size = _load_tokenizer_vocab(args.model)

    print(
        f"[bench] building synthetic prompts: shared={args.shared_len} "
        f"unique={args.unique_len} requests={n_requests}",
        file=sys.stderr,
    )
    shared_ids, unique_ids = build_token_ids(
        args.shared_len,
        args.unique_len,
        n_requests,
        vocab_size,
        seed=args.seed,
        shared_seed=args.shared_seed,
    )
    prompts = [shared_ids + u for u in unique_ids]
    prompt_len = len(prompts[0])
    print(f"[bench] prompt length per request: {prompt_len} tokens", file=sys.stderr)

    base_url = args.url.rstrip("/")
    max_conn_cap = max(n_requests * 2, 64)
    limits = httpx.Limits(
        max_connections=max_conn_cap,
        max_keepalive_connections=max_conn_cap,
    )
    async with httpx.AsyncClient(limits=limits) as client:
        # --- Warmup: send one request with the same shared prefix but a
        # distinct tail so the prefix cache is populated before the measured
        # requests fire. The 19th-request prefill cost (just the 128 unique
        # tokens) then dominates, which is the regime we want to measure.
        if args.warmup > 0:
            warm_shared, warm_uniques = build_token_ids(
                args.shared_len, args.unique_len, args.warmup, vocab_size,
                seed=args.seed - 1, shared_seed=args.shared_seed,
            )
            for w in range(args.warmup):
                warm_prompt = warm_shared + warm_uniques[w]
                t0 = time.perf_counter()
                warm = await stream_one_request(
                    client, base_url, args.model, warm_prompt,
                    max_tokens=min(8, args.max_tokens), idx=-1 - w,
                )
                t1 = time.perf_counter()
                print(
                    f"[warmup {w + 1}/{args.warmup}] {t1 - t0:.2f}s "
                    f"ttft={(warm.ttft_s or 0):.2f}s err={warm.error}",
                    file=sys.stderr,
                )

        # --- Measured run: dispatch all `n_requests` concurrently. ---
        print(
            f"[bench] dispatching {n_requests} concurrent requests "
            f"(closed-loop, concurrency={n_requests})",
            file=sys.stderr,
        )
        t_run0 = time.perf_counter()
        results: list[RequestResult] = await asyncio.gather(*[
            stream_one_request(
                client, base_url, args.model, prompts[i],
                args.max_tokens, i,
            )
            for i in range(n_requests)
        ])
        t_run = time.perf_counter() - t_run0

    # --- Aggregate ---
    successes = [r for r in results if r.error is None]
    errors = [r for r in results if r.error is not None]
    output_tokens_total = sum(r.output_tokens for r in successes)
    aggregate_throughput = output_tokens_total / t_run if t_run > 0 else 0.0

    ttfts = [r.ttft_s for r in successes if r.ttft_s is not None]
    totals = [r.total_s for r in successes]
    decodes = [r.decode_tps for r in successes if r.decode_tps > 0]

    print()
    print("=" * 60)
    print("  Prefix-caching benchmark (shared 32k + unique tail)")
    print("=" * 60)
    print(f"Backend URL:           {base_url}/v1/completions")
    print(f"Requests:              {n_requests} (completed {len(successes)}, errors {len(errors)})")
    print(f"Shared prefix tokens:  {args.shared_len}")
    print(f"Unique tail tokens:    {args.unique_len}")
    print(f"Output tokens / req:   {args.max_tokens}")
    print(f"Wall clock:            {t_run:.2f}s")
    print(f"Total output tokens:   {output_tokens_total}")
    print()

    if ttfts:
        print(
            f"TTFT (s)   mean / p50 / p95 / max: "
            f"{statistics.mean(ttfts):.2f} / {percentile(ttfts, 50):.2f} / "
            f"{percentile(ttfts, 95):.2f} / {max(ttfts):.2f}"
        )
    if totals:
        print(
            f"Total (s)  mean / p50 / p95 / max: "
            f"{statistics.mean(totals):.2f} / {percentile(totals, 50):.2f} / "
            f"{percentile(totals, 95):.2f} / {max(totals):.2f}"
        )
    if decodes:
        print(
            f"Decode tps mean / p50 / p95 / min: "
            f"{statistics.mean(decodes):.2f} / {percentile(decodes, 50):.2f} / "
            f"{percentile(decodes, 95):.2f} / {min(decodes):.2f}"
        )

    print()
    print(f"Primary metric: aggregate_throughput_tok_per_sec = {aggregate_throughput:.2f}")
    print(f"Completed: {len(successes)}/{n_requests} requests")

    if errors:
        print("\nErrors:")
        for i, r in enumerate(errors[:5]):
            print(f"  [{i}] req={r.idx} {r.error[:140]}")
        if len(errors) > 5:
            print(f"  ... and {len(errors) - 5} more")

    result_dict = {
        "config": {
            "url": f"{base_url}/v1/completions",
            "model": args.model,
            "shared_len": args.shared_len,
            "unique_len": args.unique_len,
            "max_tokens": args.max_tokens,
            "num_requests": n_requests,
            "warmup": args.warmup,
            "seed": args.seed,
            "shared_seed": args.shared_seed,
        },
        "num_requests": n_requests,
        "num_completed": len(successes),
        "num_failed": len(errors),
        "wall_clock_sec": t_run,
        "total_output_tokens": output_tokens_total,
        "aggregate_throughput_tok_per_sec": aggregate_throughput,
        "ttft_sec": _pct_block(ttfts),
        "total_latency_sec": _pct_block(totals),
        "decode_tps_per_request": _pct_block(decodes),
        "per_request": [asdict(r) for r in results],
    }

    if args.output_json:
        Path(args.output_json).write_text(json.dumps(result_dict, indent=2))
        print(f"\nResults written to {args.output_json}")

    return result_dict


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Prefix-caching workload benchmark for an OpenAI-compatible "
            "/v1/completions server. Sends N concurrent requests that share "
            "a 32 768-token prefix; reports aggregate output throughput."
        ),
    )
    p.add_argument("--url", default="http://localhost:8000", help="Server base URL")
    p.add_argument(
        "--model", default=DEFAULT_MODEL_ID,
        help="HF model id, used both for tokenizer and the `model` field in completions calls",
    )
    p.add_argument("--shared-len", type=int, default=32_768,
                   help="Shared prefix length in tokens (default 32768).")
    p.add_argument("--unique-len", type=int, default=128,
                   help="Unique tail length per request in tokens (default 128).")
    p.add_argument("--max-tokens", type=int, default=128,
                   help="Output tokens per request (default 128).")
    p.add_argument("--requests", type=int, default=20,
                   help="Number of concurrent requests (default 20).")
    # Alias for orchestrate's sanity-check invocation, which uses
    # `--num-requests 2`. Mirrors the convention from other input bundles.
    p.add_argument("--num-requests", type=int, default=None,
                   help="Alias for --requests (orchestrate sanity-check uses this name).")
    p.add_argument("--warmup", type=int, default=1,
                   help="Number of warmup requests to populate the prefix cache (default 1).")
    p.add_argument("--seed", type=int, default=int(time.time()),
                   help="RNG seed for per-request unique tails (default: wall clock).")
    p.add_argument("--shared-seed", type=int, default=0,
                   help=("RNG seed for the shared prefix. Default 0 keeps the "
                         "32k prefix identical across invocations, which is the "
                         "whole point of measuring shared-prefix caching."))
    p.add_argument("--output-json", type=str, default=None,
                   help="Optional path to write structured results.")

    # Back-compat no-ops so the orchestrate sanity / profiler invocations
    # (which default to `--rate 1 --num-requests 5 --max-tokens 64`) do not
    # choke on flags this benchmark doesn't need.
    p.add_argument("--rate", type=float, default=None,
                   help="Ignored — this benchmark is closed-loop concurrent only.")
    p.add_argument("--duration", type=float, default=None,
                   help="Ignored — this benchmark runs to --requests / --num-requests.")
    p.add_argument("--prompt-len", type=int, default=None,
                   help="Ignored — prompts are synthesised from --shared-len + --unique-len.")
    p.add_argument("--temperature", type=float, default=None,
                   help="Ignored — temperature is fixed at 0 for deterministic decoding.")
    p.add_argument("--endpoint", type=str, default=None,
                   help="Ignored — endpoint is /v1/completions.")
    p.add_argument("--audio-dir", type=str, default=None,
                   help="Ignored — text-only benchmark.")

    args = p.parse_args()
    asyncio.run(run_benchmark(args))


if __name__ == "__main__":
    main()
