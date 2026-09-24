"""
Serving performance benchmark for LLM inference servers.

Generates load following a Poisson arrival process or closed-loop concurrency
and measures TTFT, TPOT, end-to-end latency, and throughput.

Usage:
    .venv/bin/python benchmark.py --url http://localhost:8002 --rate 2 --duration 30 --max-tokens 64
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random

import httpx

from vs_bench.runner import run
from vs_bench.schedule import duration_limited, poisson
from vs_bench.stats import pct_block
from vs_bench.transport import stream_sse

PROMPT_POOL = [
    "The capital of France is",
    "Once upon a time, in a land far away,",
    'def fibonacci(n):\n    """Return the n-th Fibonacci number."""\n',
    "1 + 1 =",
    "A B C D E F G H I J K L M N O P Q R S T U V W X Y Z A B C D E F G",
    (
        "The following is a detailed explanation of how neural networks work. "
        "Neural networks are computing systems inspired by biological neural networks. "
        "They consist of layers of interconnected nodes or neurons. "
        "Each connection has a weight that adjusts as learning proceeds. "
        "The network processes information using a connectionist approach. "
        "In summary, the key takeaway is that"
    ),
    "Question: What is the speed of light?\nAnswer:",
    "The year 2024 was followed by the year",
    "Hello",
    '{"name": "Alice", "age":',
    "Explain the theory of relativity in simple terms.",
    "Write a short poem about the ocean.",
    (
        "In computer science, a hash table is a data structure that implements "
        "an associative array, also called a dictionary. A hash table uses a "
        "hash function to compute an index into an array of buckets, from which "
        "the desired value can be found. The main advantage of hash tables is"
    ),
]


async def run_benchmark(args: argparse.Namespace) -> dict:
    rng = random.Random(args.seed)
    url = args.url.rstrip("/") + args.endpoint

    if args.prompt_len is not None:
        prompt_list = [" ".join(["token"] * args.prompt_len) for _ in range(20)]
    else:
        prompt_list = list(PROMPT_POOL)

    async with httpx.AsyncClient() as client:

        async def send(i: int) -> dict:
            prompt = rng.choice(prompt_list)
            r = await stream_sse(
                client,
                url,
                {
                    "model": args.model,
                    "prompt": prompt,
                    "max_tokens": args.max_tokens,
                    "temperature": args.temperature,
                    "stream": True,
                },
                timeout=120.0,
            )
            return {
                "error": r.error,
                "output_tokens": r.token_count,
                "finish_reason": r.finish_reason,
                "total_latency": r.latency,
                "ttft": r.ttft,
                "tpot": None
                if r.ttft is None or r.token_count <= 1
                else (r.latency - r.ttft) / (r.token_count - 1),
            }

        # Warmup
        if args.warmup_requests > 0:

            async def warmup_send(index: int) -> dict[str, object]:
                return await send(index)

            from vs_bench.schedule import closed_loop

            warmup_result = await run(
                closed_loop(args.warmup_requests),
                warmup_send,
                concurrency=min(args.warmup_requests, args.concurrency or 1),
            )
            warmup_errors = [r for r in warmup_result.results if r["error"] is not None]
            print(
                f"Warm-up:           {len(warmup_result.results) - len(warmup_errors)}/"
                f"{len(warmup_result.results)} requests completed in "
                f"{warmup_result.wall_clock:.1f}s; results discarded"
            )
            if warmup_errors:
                raise RuntimeError(
                    f"{len(warmup_errors)} warm-up request(s) failed. "
                    f"First error: {warmup_errors[0]['error']}"
                )

        # Measured run
        if args.concurrency:
            schedule = duration_limited(args.duration)
            conc = args.concurrency
        else:
            n = args.num_requests if args.num_requests else 10**9
            schedule = poisson(n, args.rate, seed=args.seed)
            conc = 10000

        result = await run(schedule, send, concurrency=conc)

    wall_clock = result.wall_clock
    successes = [r for r in result.results if r["error"] is None]
    errors = [r for r in result.results if r["error"] is not None]

    ttfts = [r["ttft"] for r in successes if r["ttft"] is not None]
    tpots = [r["tpot"] for r in successes if r["tpot"] is not None]
    latencies = [r["total_latency"] for r in successes]
    total_output_tokens = sum(r["output_tokens"] for r in successes)

    print()
    print("=" * 40)
    print("  Benchmark Results")
    print("=" * 40)
    print(f"Backend URL:       {url}")
    print(f"Duration:          {wall_clock:.1f}s")
    print(
        f"Completed:         {len(successes)}/{len(result.results)} requests ({len(errors)} errors)"
    )
    print()
    print("Throughput:")
    print(f"  Request:         {len(successes) / wall_clock:.2f} req/s")
    print(f"  Token (output):  {total_output_tokens / wall_clock:.1f} tok/s")

    if errors:
        print("Errors:")
        for i, r in enumerate(errors[:5]):
            print(f"  [{i}] {r['error'][:120]}")

    result_dict = {
        "config": {
            "url": url,
            "model": args.model,
            "rate": args.rate,
            "concurrency": args.concurrency,
            "duration": args.duration,
            "max_tokens": args.max_tokens,
            "temperature": args.temperature,
            "seed": args.seed,
            "warmup_requests": args.warmup_requests,
        },
        "warmup_num_requests": args.warmup_requests,
        "num_requests": len(result.results),
        "num_completed": len(successes),
        "num_failed": len(errors),
        "actual_duration": wall_clock,
        "actual_rate": len(successes) / wall_clock if wall_clock > 0 else 0,
        "total_tokens": total_output_tokens,
        "aggregate_throughput": total_output_tokens / wall_clock if wall_clock > 0 else 0,
        "request_throughput": len(successes) / wall_clock if wall_clock > 0 else 0,
        "ttft": pct_block(ttfts, 1000.0),
        "tpot": pct_block(tpots, 1000.0),
        "total_latency": pct_block(latencies, 1000.0),
        "p99_ttft_ms": pct_block(ttfts, 1000.0)["p99"] if ttfts else None,
        "p99_tpot_ms": pct_block(tpots, 1000.0)["p99"] if tpots else None,
        "p99_latency_ms": pct_block(latencies, 1000.0)["p99"] if latencies else None,
    }

    if hasattr(args, "output_json") and args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(result_dict, f, indent=2)
        print(f"Results written to {args.output_json}")

    return result_dict


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark an OpenAI-compatible streaming completion server."
    )
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--endpoint", default="/v1/completions")
    parser.add_argument("--model", default="meta-llama/Llama-3.1-8B-Instruct")
    parser.add_argument("--rate", type=float, default=1.0)
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--num-requests", type=int, default=None)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--temperature", type=float, default=0)
    parser.add_argument("--warmup-requests", type=int, default=4)
    parser.add_argument("--prompt-len", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-json", type=str, default=None)
    args = parser.parse_args()
    if args.concurrency is not None and args.concurrency <= 0:
        parser.error("--concurrency must be greater than zero")
    asyncio.run(run_benchmark(args))


if __name__ == "__main__":
    main()
