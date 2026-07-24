from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import time
from typing import Any

import httpx


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def stats(values: list[float], multiplier: float = 1000.0) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "mean": sum(ordered) / len(ordered) * multiplier,
        "p50": percentile(ordered, 50) * multiplier,
        "p90": percentile(ordered, 90) * multiplier,
        "p95": percentile(ordered, 95) * multiplier,
        "p99": percentile(ordered, 99) * multiplier,
    }


def prompts(prompt_len: int, pool_size: int) -> list[str]:
    base = " ".join(f"fact{i % 97}" for i in range(prompt_len))
    return [base for _ in range(pool_size)]


async def send_request(
    client: httpx.AsyncClient,
    url: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> dict[str, Any]:
    body = {
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    started = time.perf_counter()
    first_token = None
    done = None
    output_tokens = 0
    try:
        async with client.stream("POST", url, json=body, timeout=timeout) as response:
            response.raise_for_status()
            async for raw_line in response.aiter_lines():
                if not raw_line.startswith("data: "):
                    continue
                payload = raw_line[len("data: ") :].strip()
                if payload == "[DONE]":
                    done = time.perf_counter()
                    break
                chunk = json.loads(payload)
                text = (chunk.get("choices") or [{}])[0].get("text") or ""
                if text:
                    output_tokens += 1
                    if first_token is None:
                        first_token = time.perf_counter()
    except Exception as exc:  # noqa: BLE001
        return {
            "error": str(exc),
            "total_latency": time.perf_counter() - started,
            "ttft": None,
            "tpot": None,
            "output_tokens": output_tokens,
        }
    if done is None:
        done = time.perf_counter()
    return {
        "error": None,
        "total_latency": done - started,
        "ttft": None if first_token is None else first_token - started,
        "tpot": None
        if first_token is None or output_tokens <= 1
        else (done - first_token) / (output_tokens - 1),
        "output_tokens": output_tokens,
    }


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    rng = random.Random(args.seed)
    prompt_pool = prompts(args.prompt_len, args.prompt_pool_size)
    url = args.url.rstrip("/") + args.endpoint
    results: list[dict[str, Any]] = []
    started = time.perf_counter()

    async with httpx.AsyncClient() as client:

        async def worker() -> None:
            while time.perf_counter() - started < args.duration:
                prompt = rng.choice(prompt_pool)
                results.append(
                    await send_request(
                        client,
                        url,
                        prompt,
                        args.max_tokens,
                        args.temperature,
                        args.timeout,
                    )
                )

        await asyncio.gather(*(worker() for _ in range(args.concurrency)))

    wall = time.perf_counter() - started
    successes = [r for r in results if r["error"] is None]
    errors = [r for r in results if r["error"] is not None]
    ttft = stats([r["ttft"] for r in successes if r["ttft"] is not None])
    tpot = stats([r["tpot"] for r in successes if r["tpot"] is not None])
    latency = stats([r["total_latency"] for r in successes])
    total_tokens = sum(r["output_tokens"] for r in successes)
    output = {
        "config": {
            "url": url,
            "concurrency": args.concurrency,
            "duration": args.duration,
            "max_tokens": args.max_tokens,
            "temperature": args.temperature,
            "prompt_len": args.prompt_len,
            "prompt_pool_size": args.prompt_pool_size,
            "seed": args.seed,
        },
        "num_requests": len(results),
        "num_completed": len(successes),
        "num_failed": len(errors),
        "actual_duration": wall,
        "total_tokens": total_tokens,
        "aggregate_throughput": total_tokens / wall if wall > 0 else 0,
        "request_throughput": len(successes) / wall if wall > 0 else 0,
        "ttft": ttft,
        "tpot": tpot,
        "total_latency": latency,
        "p99_ttft_ms": None if ttft is None else ttft["p99"],
        "p99_tpot_ms": None if tpot is None else tpot["p99"],
        "p99_latency_ms": None if latency is None else latency["p99"],
        "errors": errors[:5],
    }

    print(f"Completed {len(successes)}/{len(results)} requests")
    print(f"Aggregate throughput: {output['aggregate_throughput']:.2f} tok/s")
    if output["p99_latency_ms"] is not None:
        print(f"p99 latency: {output['p99_latency_ms']:.2f} ms")
    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(output, f, indent=2)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Long-prompt short-output vLLM benchmark.")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--endpoint", default="/v1/completions")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--duration", type=float, default=20)
    parser.add_argument("--max-tokens", type=int, default=16)
    parser.add_argument("--temperature", type=float, default=0)
    parser.add_argument("--prompt-len", type=int, default=3000)
    parser.add_argument("--prompt-pool-size", type=int, default=64)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output-json", default=None)
    args = parser.parse_args()
    asyncio.run(run_benchmark(args))


if __name__ == "__main__":
    main()
