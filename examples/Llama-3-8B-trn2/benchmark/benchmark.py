"""
Serving performance benchmark for LLM inference servers.

Generates load following a Poisson arrival process and measures:
- Time to First Token (TTFT)
- Time per Output Token (TPOT)
- End-to-end latency
- Request and token throughput

Usage:
    .venv/bin/python benchmark.py --url http://localhost:8002 --rate 2 --duration 30 --max-tokens 64
"""

import argparse
import asyncio
import json
import math
import random
import time

import httpx

# ---------------------------------------------------------------------------
# Prompt pool — diverse prompts reused from accuracy_checker.py + extras
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Per-request measurement
# ---------------------------------------------------------------------------


async def send_request(
    client: httpx.AsyncClient,
    url: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
) -> dict:
    """Send a single streaming completion request and measure timings."""
    body = {
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }

    t_send = time.perf_counter()
    t_first_token = None
    t_done = None
    output_tokens = 0
    finish_reason = None
    error = None

    try:
        async with client.stream("POST", url, json=body, timeout=120.0) as resp:
            resp.raise_for_status()
            async for raw_line in resp.aiter_lines():
                if not raw_line.startswith("data: "):
                    continue
                payload = raw_line[len("data: "):]
                if payload.strip() == "[DONE]":
                    t_done = time.perf_counter()
                    break
                chunk = json.loads(payload)
                text = chunk["choices"][0]["text"]
                reason = chunk["choices"][0].get("finish_reason")
                if reason is not None:
                    finish_reason = reason
                if text:
                    output_tokens += 1
                    if t_first_token is None:
                        t_first_token = time.perf_counter()
    except Exception as exc:
        error = str(exc)
        t_done = time.perf_counter()

    # Compute metrics
    if t_done is None:
        t_done = time.perf_counter()

    result: dict = {
        "error": error,
        "output_tokens": output_tokens,
        "finish_reason": finish_reason,
        "total_latency": t_done - t_send,
    }

    if t_first_token is not None:
        result["ttft"] = t_first_token - t_send
        if output_tokens > 1:
            result["tpot"] = (t_done - t_first_token) / (output_tokens - 1)
        else:
            result["tpot"] = None
    else:
        result["ttft"] = None
        result["tpot"] = None

    return result


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def percentile(sorted_vals: list[float], p: float) -> float:
    """Return the p-th percentile (0-100) from a pre-sorted list."""
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def format_stats(values: list[float], unit: str = "ms", multiplier: float = 1000.0) -> str:
    """Format mean / median / p90 / p99 for a list of values."""
    if not values:
        return "    (no data)\n"
    s = sorted(values)
    mean = sum(s) / len(s)
    median = percentile(s, 50)
    p90 = percentile(s, 90)
    p99 = percentile(s, 99)
    return (
        f"  Mean:    {mean * multiplier:.1f} {unit}\n"
        f"  Median:  {median * multiplier:.1f} {unit}\n"
        f"  P90:     {p90 * multiplier:.1f} {unit}\n"
        f"  P99:     {p99 * multiplier:.1f} {unit}\n"
    )


# ---------------------------------------------------------------------------
# Main benchmark driver
# ---------------------------------------------------------------------------


async def run_benchmark(args: argparse.Namespace) -> dict:
    rng = random.Random(args.seed)
    url = args.url.rstrip("/") + args.endpoint

    # Build prompt list
    if args.prompt_len is not None:
        # Synthetic prompts: repeat a filler word to approximate token count
        prompts = [" ".join(["token"] * args.prompt_len) for _ in range(20)]
    else:
        prompts = list(PROMPT_POOL)

    # Determine stopping condition
    use_duration = args.num_requests is None
    total_requests = args.num_requests if not use_duration else 10**9

    tasks: list[asyncio.Task] = []
    results: list[dict] = []
    sent = 0

    async with httpx.AsyncClient() as client:
        t_bench_start = time.perf_counter()

        while sent < total_requests:
            # Check duration limit
            if use_duration and (time.perf_counter() - t_bench_start) >= args.duration:
                break

            prompt = rng.choice(prompts)
            task = asyncio.create_task(
                send_request(client, url, prompt, args.max_tokens, args.temperature)
            )
            tasks.append(task)
            sent += 1

            # Poisson inter-arrival delay
            if sent < total_requests:
                delay = -math.log(1.0 - rng.random()) / args.rate
                # Cap delay so we don't overshoot duration
                if use_duration:
                    remaining = args.duration - (time.perf_counter() - t_bench_start)
                    if remaining <= 0:
                        break
                    delay = min(delay, remaining)
                await asyncio.sleep(delay)

        # Wait for in-flight requests to finish
        results = await asyncio.gather(*tasks)
        t_bench_end = time.perf_counter()

    wall_clock = t_bench_end - t_bench_start

    # Separate successes and errors
    successes = [r for r in results if r["error"] is None]
    errors = [r for r in results if r["error"] is not None]

    ttfts = [r["ttft"] for r in successes if r["ttft"] is not None]
    tpots = [r["tpot"] for r in successes if r["tpot"] is not None]
    latencies = [r["total_latency"] for r in successes]
    total_output_tokens = sum(r["output_tokens"] for r in successes)

    # Print results
    print()
    print("=" * 40)
    print("  Benchmark Results")
    print("=" * 40)
    print(f"Backend URL:       {args.url.rstrip('/')}{args.endpoint}")
    print(f"Duration:          {wall_clock:.1f}s")
    print(f"Completed:         {len(successes)}/{len(results)} requests ({len(errors)} errors)")
    print()
    print("Throughput:")
    print(f"  Request:         {len(successes) / wall_clock:.2f} req/s")
    print(f"  Token (output):  {total_output_tokens / wall_clock:.1f} tok/s")
    print()
    print("Time to First Token (TTFT):")
    print(format_stats(ttfts))
    print("Time per Output Token (TPOT):")
    print(format_stats(tpots))
    print("Total Latency (end-to-end):")
    print(format_stats(latencies))

    if errors:
        print("Errors:")
        for i, r in enumerate(errors[:5]):
            print(f"  [{i}] {r['error'][:120]}")
        if len(errors) > 5:
            print(f"  ... and {len(errors) - 5} more")
        print()

    # Build structured results dict
    sorted_ttfts = sorted(ttfts) if ttfts else []
    sorted_tpots = sorted(tpots) if tpots else []
    sorted_lats = sorted(latencies) if latencies else []

    result_dict = {
        "config": {
            "url": args.url.rstrip("/") + args.endpoint,
            "rate": args.rate,
            "duration": args.duration,
            "max_tokens": args.max_tokens,
            "temperature": args.temperature,
            "seed": args.seed,
        },
        "num_requests": len(results),
        "num_completed": len(successes),
        "num_failed": len(errors),
        "actual_duration": wall_clock,
        "actual_rate": len(successes) / wall_clock if wall_clock > 0 else 0,
        "total_tokens": total_output_tokens,
        "aggregate_throughput": total_output_tokens / wall_clock if wall_clock > 0 else 0,
        "request_throughput": len(successes) / wall_clock if wall_clock > 0 else 0,
    }

    def _pct_block(sorted_vals):
        if not sorted_vals:
            return None
        return {
            "mean": sum(sorted_vals) / len(sorted_vals) * 1000,
            "p50": percentile(sorted_vals, 50) * 1000,
            "p90": percentile(sorted_vals, 90) * 1000,
            "p95": percentile(sorted_vals, 95) * 1000,
            "p99": percentile(sorted_vals, 99) * 1000,
        }

    result_dict["ttft"] = _pct_block(sorted_ttfts)
    result_dict["tpot"] = _pct_block(sorted_tpots)
    result_dict["total_latency"] = _pct_block(sorted_lats)

    if hasattr(args, "output_json") and args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(result_dict, f, indent=2)
        print(f"Results written to {args.output_json}")

    return result_dict


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark an OpenAI-compatible streaming completion server."
    )
    parser.add_argument("--url", default="http://localhost:8000", help="Server base URL")
    parser.add_argument("--endpoint", default="/v1/completions", help="API endpoint path")
    parser.add_argument("--rate", type=float, default=1.0, help="Request rate (req/s, Poisson)")
    parser.add_argument("--duration", type=float, default=60, help="Benchmark duration in seconds")
    parser.add_argument(
        "--num-requests", type=int, default=None, help="Total requests (overrides --duration)"
    )
    parser.add_argument("--max-tokens", type=int, default=128, help="Max tokens per request")
    parser.add_argument("--temperature", type=float, default=0, help="Sampling temperature")
    parser.add_argument(
        "--prompt-len",
        type=int,
        default=None,
        help="If set, use synthetic prompts of this token length",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument(
        "--output-json",
        type=str,
        default=None,
        help="If set, write structured results to this JSON file path",
    )
    args = parser.parse_args()
    asyncio.run(run_benchmark(args))


if __name__ == "__main__":
    main()