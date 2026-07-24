from __future__ import annotations

import argparse
import asyncio
import collections
import json
import math
import random
import statistics
import sys
import time
from typing import Any

import httpx


PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer", "minimum": 0, "maximum": 120},
        "city": {"type": "string"},
        "interests": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 4,
        },
        "active": {"type": "boolean"},
    },
    "required": ["name", "age", "city", "interests", "active"],
    "additionalProperties": False,
}

PROFILE_PROMPTS = [
    "Return only valid JSON for a fictional profile of a software engineer.",
    "Return only valid JSON for a fictional profile of a teacher.",
    "Return only valid JSON for a fictional profile of a chef.",
    "Return only valid JSON for a fictional profile of a nurse.",
    "Return only valid JSON for a fictional profile of a musician.",
]


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def stats(values: list[float], multiplier: float = 1.0) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "mean": sum(ordered) / len(ordered) * multiplier,
        "median": percentile(ordered, 50) * multiplier,
        "min": ordered[0] * multiplier,
        "max": ordered[-1] * multiplier,
        "p25": percentile(ordered, 25) * multiplier,
        "p75": percentile(ordered, 75) * multiplier,
        "p90": percentile(ordered, 90) * multiplier,
        "p99": percentile(ordered, 99) * multiplier,
    }


def validate_profile(text: str) -> str | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"invalid JSON: {exc}"
    if not isinstance(payload, dict):
        return "profile is not an object"
    required = set(PROFILE_SCHEMA["required"])
    if set(payload) != required:
        return f"keys mismatch: {sorted(payload)}"
    if not isinstance(payload["name"], str) or not payload["name"]:
        return "name must be a non-empty string"
    if not isinstance(payload["city"], str) or not payload["city"]:
        return "city must be a non-empty string"
    if (
        not isinstance(payload["age"], int)
        or isinstance(payload["age"], bool)
        or payload["age"] < 0
        or payload["age"] > 120
    ):
        return "age must be an integer in [0, 120]"
    if not isinstance(payload["active"], bool):
        return "active must be a boolean"
    interests = payload["interests"]
    if not isinstance(interests, list) or not 2 <= len(interests) <= 4:
        return "interests must have 2-4 items"
    if any(not isinstance(item, str) or not item for item in interests):
        return "interests must be non-empty strings"
    return None


async def fetch_health(client: httpx.AsyncClient, base_url: str) -> dict[str, Any]:
    response = await client.get(base_url.rstrip("/") + "/health", timeout=30.0)
    response.raise_for_status()
    return response.json()


async def send_request(
    client: httpx.AsyncClient,
    url: str,
    prompt: str,
    max_tokens: int,
    timeout: float,
) -> dict[str, Any]:
    body = {
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
        "guided_json": PROFILE_SCHEMA,
    }
    started = time.perf_counter()
    try:
        response = await client.post(url, json=body, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        completed = time.perf_counter()
        return {
            "error": type(exc).__name__ + (f": {exc}" if str(exc) else ""),
            "latency": completed - started,
            "completed_at": completed,
            "completion_tokens": 0,
            "chars": 0,
            "schema_error": None,
            "instance_id": None,
        }

    completed = time.perf_counter()
    usage = payload.get("usage") or {}
    text = ((payload.get("choices") or [{}])[0].get("text") or "")
    completion_tokens = usage.get("completion_tokens")
    if not isinstance(completion_tokens, int):
        return {
            "error": "missing usage.completion_tokens",
            "latency": completed - started,
            "completed_at": completed,
            "completion_tokens": 0,
            "chars": len(text),
            "schema_error": None,
            "instance_id": response.headers.get("x-benchmark-instance"),
        }
    schema_error = validate_profile(text)
    return {
        "error": None,
        "latency": completed - started,
        "completed_at": completed,
        "completion_tokens": completion_tokens,
        "chars": len(text),
        "schema_error": schema_error,
        "instance_id": response.headers.get("x-benchmark-instance"),
    }


async def run_closed_loop(
    *,
    url: str,
    base_url: str,
    concurrency: int,
    duration: float,
    max_tokens: int,
    timeout: float,
    seed: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    deadline = started + duration
    results: list[dict[str, Any]] = []
    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
        keepalive_expiry=60.0,
    )

    async with httpx.AsyncClient(limits=limits) as client:
        health_before = await fetch_health(client, base_url)

        async def worker(worker_id: int) -> None:
            rng = random.Random(seed + worker_id * 1_000_003)
            while time.perf_counter() < deadline:
                result = await send_request(
                    client,
                    url,
                    rng.choice(PROFILE_PROMPTS),
                    max_tokens,
                    timeout,
                )
                results.append(result)

        await asyncio.gather(*(worker(worker_id) for worker_id in range(concurrency)))
        health_after = await fetch_health(client, base_url)

    wall = time.perf_counter() - started
    in_window = [r for r in results if r["completed_at"] <= deadline]
    tail = [r for r in results if r["completed_at"] > deadline]
    request_errors = [r for r in in_window if r["error"] is not None]
    schema_errors = [r for r in in_window if r["schema_error"] is not None]
    successes = [
        r for r in in_window if r["error"] is None and r["schema_error"] is None
    ]
    completion_tokens = sum(r["completion_tokens"] for r in successes)
    chars = sum(r["chars"] for r in successes)
    success_latencies = [r["latency"] for r in successes]
    all_latencies = [r["latency"] for r in in_window]
    instances = sorted(
        {str(r["instance_id"]) for r in successes if r.get("instance_id")}
    )
    return {
        "duration": duration,
        "wall_time_with_drain": wall,
        "num_requests": len(results),
        "num_in_window": len(in_window),
        "num_tail": len(tail),
        "num_completed": len(successes),
        "num_request_failed": len(request_errors),
        "num_schema_failed": len(schema_errors),
        "completion_tokens": completion_tokens,
        "chars": chars,
        "token_throughput": completion_tokens / duration if duration > 0 else 0,
        "char_throughput": chars / duration if duration > 0 else 0,
        "request_throughput": len(successes) / duration if duration > 0 else 0,
        "tokens_per_request": completion_tokens / len(successes) if successes else 0,
        "chars_per_request": chars / len(successes) if successes else 0,
        "success_latency_ms": stats(success_latencies, multiplier=1000.0),
        "all_latency_ms": stats(all_latencies, multiplier=1000.0),
        "request_error_counts": dict(
            collections.Counter(str(r["error"]) for r in request_errors)
        ),
        "schema_error_counts": dict(
            collections.Counter(str(r["schema_error"]) for r in schema_errors)
        ),
        "sample_errors": (request_errors + schema_errors)[:5],
        "instance_ids": instances,
        "health_before": health_before,
        "health_after": health_after,
    }


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    base_url = args.url.rstrip("/")
    url = base_url + args.endpoint
    warmup = await run_closed_loop(
        url=url,
        base_url=base_url,
        concurrency=args.concurrency,
        duration=args.warmup_duration,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
        seed=args.seed,
    )

    trials = []
    for trial_idx in range(args.trials):
        trial = await run_closed_loop(
            url=url,
            base_url=base_url,
            concurrency=args.concurrency,
            duration=args.trial_duration,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            seed=args.seed + trial_idx + 1,
        )
        trial["trial"] = trial_idx + 1
        trials.append(trial)
        print(
            "Trial "
            f"{trial_idx + 1}: {trial['token_throughput']:.2f} tok/s, "
            f"{trial['request_throughput']:.2f} req/s, "
            f"{trial['tokens_per_request']:.2f} tok/req",
            flush=True,
        )

    token_rates = [t["token_throughput"] for t in trials]
    request_rates = [t["request_throughput"] for t in trials]
    failed_trials = [
        t
        for t in trials
        if t["num_schema_failed"]
        or t["num_completed"] == 0
        or (
            t["num_request_failed"] / t["num_in_window"]
            if t["num_in_window"]
            else 1.0
        )
        > args.max_request_error_rate
    ]
    unstable_instance_trials = [
        t
        for t in trials
        if t["health_before"].get("run_instance_id")
        != t["health_after"].get("run_instance_id")
        or len(t["instance_ids"]) > 1
    ]
    missing_instance_trials = [t for t in trials if not t["instance_ids"]]
    output = {
        "config": {
            "url": url,
            "concurrency": args.concurrency,
            "warmup_duration": args.warmup_duration,
            "trial_duration": args.trial_duration,
            "trials": args.trials,
            "max_tokens": args.max_tokens,
            "seed": args.seed,
            "max_request_error_rate": args.max_request_error_rate,
            "schema": PROFILE_SCHEMA,
            "prompts": PROFILE_PROMPTS,
            "http_limits": {
                "max_connections": args.concurrency,
                "max_keepalive_connections": args.concurrency,
                "keepalive_expiry": 60.0,
            },
        },
        "warmup": warmup,
        "trials": trials,
        "summary": {
            "token_throughput": stats(token_rates),
            "request_throughput": stats(request_rates),
            "token_throughput_stdev": (
                statistics.stdev(token_rates) if len(token_rates) > 1 else 0
            ),
            "request_throughput_stdev": (
                statistics.stdev(request_rates) if len(request_rates) > 1 else 0
            ),
            "failed_trials": len(failed_trials),
            "unstable_instance_trials": len(unstable_instance_trials),
            "missing_instance_trials": len(missing_instance_trials),
            "valid": (
                not failed_trials
                and not unstable_instance_trials
                and not missing_instance_trials
            ),
        },
    }
    print(
        "Median throughput: "
        f"{output['summary']['token_throughput']['median']:.2f} tok/s",
        flush=True,
    )
    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(output, f, indent=2)
    return output


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Robust constrained JSON token benchmark."
    )
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--endpoint", default="/v1/completions")
    parser.add_argument("--concurrency", type=positive_int, default=16)
    parser.add_argument("--warmup-duration", type=positive_float, default=20)
    parser.add_argument("--trial-duration", type=positive_float, default=60)
    parser.add_argument("--trials", type=positive_int, default=5)
    parser.add_argument("--max-tokens", type=positive_int, default=96)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--max-request-error-rate", type=float, default=0.005)
    parser.add_argument("--output-json", default=None)
    args = parser.parse_args()
    output = asyncio.run(run_benchmark(args))
    if not output["summary"]["valid"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
