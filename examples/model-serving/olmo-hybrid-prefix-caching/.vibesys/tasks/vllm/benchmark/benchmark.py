"""
Prefix-caching workload benchmark for an OpenAI-compatible /v1/completions
server.

Workload (per request):
  - 32 768 tokens of shared prefix (identical across every request).
  - 128 tokens of unique tail.
  - 128 tokens of generation, temperature 0, ignore_eos.

The 20 requests are dispatched concurrently. The headline metric is the
**aggregate output throughput** (sum of all output tokens / wall clock).

This benchmark synthesises prompts as raw token IDs and sends them via
``prompt: list[int]`` (vLLM-compatible).

Usage:
    python benchmark.py --url http://localhost:8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from vs_bench.stats import pct_block
from vs_bench.transport import stream_sse

DEFAULT_MODEL_ID = "allenai/Olmo-Hybrid-7B"


@dataclass
class RequestResult:
    idx: int
    ttft_s: float | None
    total_s: float
    output_tokens: int
    decode_tps: float
    error: str | None


def build_token_ids(
    shared_len: int,
    unique_len: int,
    n_requests: int,
    vocab_size: int,
    seed: int,
    shared_seed: int,
) -> tuple[list[int], list[list[int]]]:
    lo, hi = 100, max(101, vocab_size - 100)
    shared_rng = random.Random(shared_seed)
    shared = [shared_rng.randint(lo, hi) for _ in range(shared_len)]
    uniques: list[list[int]] = []
    for r in range(n_requests):
        rr = random.Random(seed + 1 + r)
        uniques.append([rr.randint(lo, hi) for _ in range(unique_len)])
    return shared, uniques


def _load_tokenizer_vocab(model_id: str) -> int:
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise SystemExit("transformers is required: pip install transformers") from exc
    tok = AutoTokenizer.from_pretrained(model_id)
    return tok.vocab_size


async def stream_one_request(
    client: httpx.AsyncClient,
    url: str,
    model_id: str,
    token_ids: list[int],
    max_tokens: int,
    idx: int,
) -> RequestResult:
    body = {
        "model": model_id,
        "prompt": token_ids,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "ignore_eos": True,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    r = await stream_sse(client, url, body, timeout=600.0)

    if r.error:
        return RequestResult(
            idx=idx, ttft_s=None, total_s=r.latency, output_tokens=0, decode_tps=0.0, error=r.error
        )

    gen = (
        r.usage["completion_tokens"]
        if r.usage and "completion_tokens" in r.usage
        else r.token_count
    )
    decode_window = max(r.latency - (r.ttft or 0), 1e-6)
    decode_tps = gen / decode_window if gen else 0.0
    return RequestResult(
        idx=idx,
        ttft_s=r.ttft,
        total_s=r.latency,
        output_tokens=gen,
        decode_tps=decode_tps,
        error=None,
    )


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
    limits = httpx.Limits(max_connections=max_conn_cap, max_keepalive_connections=max_conn_cap)

    async with httpx.AsyncClient(limits=limits) as client:
        # Warmup
        if args.warmup > 0:
            warm_shared, warm_uniques = build_token_ids(
                args.shared_len,
                args.unique_len,
                args.warmup,
                vocab_size,
                seed=args.seed - 1,
                shared_seed=args.shared_seed,
            )
            for w in range(args.warmup):
                warm_prompt = warm_shared + warm_uniques[w]
                t0 = time.perf_counter()
                warm = await stream_one_request(
                    client,
                    base_url,
                    args.model,
                    warm_prompt,
                    max_tokens=min(8, args.max_tokens),
                    idx=-1 - w,
                )
                t1 = time.perf_counter()
                print(
                    f"[warmup {w + 1}/{args.warmup}] {t1 - t0:.2f}s "
                    f"ttft={(warm.ttft_s or 0):.2f}s err={warm.error}",
                    file=sys.stderr,
                )

        # Measured run: all requests concurrent
        print(
            f"[bench] dispatching {n_requests} concurrent requests",
            file=sys.stderr,
        )
        t_run0 = time.perf_counter()
        results: list[RequestResult] = await asyncio.gather(
            *[
                stream_one_request(client, base_url, args.model, prompts[i], args.max_tokens, i)
                for i in range(n_requests)
            ]
        )
        t_run = time.perf_counter() - t_run0

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
            f"{statistics.mean(ttfts):.2f} / {pct_block(ttfts)['p50']:.2f} / "
            f"{pct_block(ttfts)['p95']:.2f} / {max(ttfts):.2f}"
        )
    if totals:
        print(
            f"Total (s)  mean / p50 / p95 / max: "
            f"{statistics.mean(totals):.2f} / {pct_block(totals)['p50']:.2f} / "
            f"{pct_block(totals)['p95']:.2f} / {max(totals):.2f}"
        )
    if decodes:
        print(
            f"Decode tps mean / p50 / p95 / min: "
            f"{statistics.mean(decodes):.2f} / {pct_block(decodes)['p50']:.2f} / "
            f"{pct_block(decodes)['p95']:.2f} / {min(decodes):.2f}"
        )

    print()
    print(f"Primary metric: aggregate_throughput_tok_per_sec = {aggregate_throughput:.2f}")
    print(f"Completed: {len(successes)}/{n_requests} requests")

    if errors:
        print("\nErrors:")
        for i, r in enumerate(errors[:5]):
            print(f"  [{i}] req={r.idx} {r.error[:140]}")

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
        "ttft_sec": pct_block(ttfts),
        "total_latency_sec": pct_block(totals),
        "decode_tps_per_request": pct_block(decodes),
        "per_request": [asdict(r) for r in results],
    }

    if args.output_json:
        Path(args.output_json).write_text(json.dumps(result_dict, indent=2))
        print(f"\nResults written to {args.output_json}")

    return result_dict


def main() -> None:
    p = argparse.ArgumentParser(
        description="Prefix-caching workload benchmark for an OpenAI-compatible server.",
    )
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--model", default=DEFAULT_MODEL_ID)
    p.add_argument("--shared-len", type=int, default=32_768)
    p.add_argument("--unique-len", type=int, default=128)
    p.add_argument("--max-tokens", type=int, default=128)
    p.add_argument("--requests", type=int, default=20)
    p.add_argument("--num-requests", type=int, default=None)
    p.add_argument("--warmup", type=int, default=1)
    p.add_argument("--seed", type=int, default=int(time.time()))
    p.add_argument("--shared-seed", type=int, default=0)
    p.add_argument("--output-json", type=str, default=None)
    # Back-compat no-ops
    p.add_argument("--rate", type=float, default=None)
    p.add_argument("--duration", type=float, default=None)
    p.add_argument("--prompt-len", type=int, default=None)
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--endpoint", type=str, default=None)
    p.add_argument("--audio-dir", type=str, default=None)
    args = p.parse_args()
    asyncio.run(run_benchmark(args))


if __name__ == "__main__":
    main()
