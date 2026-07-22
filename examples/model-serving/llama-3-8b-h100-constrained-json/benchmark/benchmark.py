from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import time
from typing import Any

import httpx
from jsonschema.validators import validator_for

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

PROMPTS = [
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


def validate(text: str) -> tuple[bool, bool, str | None]:
    try:
        value = json.loads(text)
    except Exception as exc:
        return False, False, str(exc)
    try:
        cls = validator_for(PROFILE_SCHEMA)
        cls.check_schema(PROFILE_SCHEMA)
        cls(PROFILE_SCHEMA).validate(value)
    except Exception as exc:
        return True, False, str(exc)
    return True, True, None


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
        "stream": True,
        "guided_json": PROFILE_SCHEMA,
    }
    started = time.perf_counter()
    first_token = None
    done = None
    output_tokens = 0
    pieces: list[str] = []
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
                    pieces.append(text)
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
            "parse_ok": False,
            "schema_ok": False,
        }
    if done is None:
        done = time.perf_counter()
    text = "".join(pieces)
    parse_ok, schema_ok, validation_error = validate(text)
    return {
        "error": None,
        "total_latency": done - started,
        "ttft": None if first_token is None else first_token - started,
        "tpot": None
        if first_token is None or output_tokens <= 1
        else (done - first_token) / (output_tokens - 1),
        "output_tokens": output_tokens,
        "parse_ok": parse_ok,
        "schema_ok": schema_ok,
        "validation_error": validation_error,
        "output_preview": text[:200],
    }


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    rng = random.Random(args.seed)
    url = args.url.rstrip("/") + args.endpoint
    results: list[dict[str, Any]] = []
    started = time.perf_counter()

    async with httpx.AsyncClient() as client:

        async def worker() -> None:
            while time.perf_counter() - started < args.duration:
                prompt = rng.choice(PROMPTS)
                results.append(
                    await send_request(client, url, prompt, args.max_tokens, args.timeout)
                )

        await asyncio.gather(*(worker() for _ in range(args.concurrency)))

    wall = time.perf_counter() - started
    transport_successes = [r for r in results if r["error"] is None]
    schema_successes = [r for r in transport_successes if r["schema_ok"]]
    errors = [r for r in results if r["error"] is not None]
    invalid = [r for r in transport_successes if not r["schema_ok"]]
    ttft = stats([r["ttft"] for r in schema_successes if r["ttft"] is not None])
    tpot = stats([r["tpot"] for r in schema_successes if r["tpot"] is not None])
    latency = stats([r["total_latency"] for r in schema_successes])
    total_tokens = sum(r["output_tokens"] for r in schema_successes)
    output = {
        "config": {
            "url": url,
            "concurrency": args.concurrency,
            "duration": args.duration,
            "max_tokens": args.max_tokens,
            "seed": args.seed,
        },
        "num_requests": len(results),
        "num_completed": len(transport_successes),
        "num_failed": len(errors),
        "parse_ok": sum(1 for r in transport_successes if r["parse_ok"]),
        "schema_ok": len(schema_successes),
        "schema_ok_frac": len(schema_successes) / len(transport_successes)
        if transport_successes
        else 0,
        "actual_duration": wall,
        "total_tokens": total_tokens,
        "aggregate_throughput": total_tokens / wall if wall > 0 else 0,
        "request_throughput": len(schema_successes) / wall if wall > 0 else 0,
        "ttft": ttft,
        "tpot": tpot,
        "total_latency": latency,
        "p99_ttft_ms": None if ttft is None else ttft["p99"],
        "p99_tpot_ms": None if tpot is None else tpot["p99"],
        "p99_latency_ms": None if latency is None else latency["p99"],
        "errors": errors[:5],
        "invalid": invalid[:5],
    }

    print(f"Transport completed {len(transport_successes)}/{len(results)} requests")
    print(f"Schema-valid responses: {len(schema_successes)}/{len(transport_successes)}")
    print(f"Schema-valid throughput: {output['aggregate_throughput']:.2f} tok/s")
    if output["p99_latency_ms"] is not None:
        print(f"p99 latency: {output['p99_latency_ms']:.2f} ms")
    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(output, f, indent=2)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Constrained JSON vLLM benchmark.")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--endpoint", default="/v1/completions")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--duration", type=float, default=20)
    parser.add_argument("--max-tokens", type=int, default=96)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output-json", default=None)
    args = parser.parse_args()
    asyncio.run(run_benchmark(args))


if __name__ == "__main__":
    main()
