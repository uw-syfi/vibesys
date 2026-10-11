"""JSONSchemaBench benchmark for an MLX 8-bit Llama server."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from pathlib import Path
from typing import Any

import httpx
from jsonschema.validators import validator_for

from vs_bench.runner import run
from vs_bench.schedule import closed_loop
from vs_bench.stats import pct_block
from vs_bench.transport import stream_sse

BUNDLE_DIR = Path(__file__).resolve().parents[1]
DATASET_ID = "epfl-dlab/JSONSchemaBench"
DATASET_REVISION = "5bd0f4640badc6f3f02df796421d21cb0ca0b141"


def load_cases(
    subset: str,
    split: str,
    limit: int | None,
    seed: int,
    revision: str,
    cache_dir: Path | None,
) -> list[dict[str, Any]]:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit("The `datasets` package is required to load JSONSchemaBench.") from exc

    ds = load_dataset(
        DATASET_ID,
        subset,
        split=split,
        revision=revision,
        cache_dir=str(cache_dir) if cache_dir is not None else None,
    )
    indices = list(range(len(ds)))
    if limit is not None and limit < len(indices):
        rng = random.Random(seed)
        indices = rng.sample(indices, k=limit)

    cases = []
    for idx in indices:
        row = ds[idx]
        raw_schema = row.get("json_schema") or row.get("schema") or row.get("content")
        if raw_schema is None:
            continue
        schema = json.loads(raw_schema) if isinstance(raw_schema, str) else raw_schema
        cases.append(
            {
                "unique_id": str(row.get("unique_id") or row.get("id") or idx),
                "description": row.get("description")
                or row.get("title")
                or schema.get("description")
                or "",
                "schema": schema,
            }
        )
    if not cases:
        raise SystemExit(f"No schema cases found in {DATASET_ID}:{subset} split={split}")
    return cases


def build_prompt(schema: dict, description: str) -> str:
    return (
        f"Task: {description or 'Generate one JSON value.'}\n\n"
        "Generate one JSON value that satisfies this JSON Schema:\n\n"
        f"{json.dumps(schema, indent=2, sort_keys=True)}\n\n"
        "Respond with JSON only. No prose, no markdown fences."
    )


def validate(text: str, schema: dict) -> tuple[bool, bool, str | None]:
    try:
        value = json.loads(text)
    except Exception as exc:
        return False, False, str(exc)
    try:
        cls = validator_for(schema)
        cls.check_schema(schema)
        cls(schema).validate(value)
    except Exception as exc:
        return True, False, str(exc)
    return True, True, None


async def run_benchmark(args: argparse.Namespace) -> dict:
    cases = load_cases(
        args.dataset_subset,
        args.split,
        args.limit,
        args.seed,
        args.dataset_revision,
        args.dataset_cache_dir,
    )
    url = args.url.rstrip("/") + args.endpoint

    async with httpx.AsyncClient() as client:

        async def send(i: int) -> dict:
            case = cases[i]
            schema = case["schema"]
            body = {
                "prompt": build_prompt(schema, case.get("description", "")),
                "max_tokens": args.max_tokens,
                "temperature": 0,
                "stream": True,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"schema": schema},
                },
            }
            r = await stream_sse(client, url, body, timeout=args.timeout)
            if r.error:
                return {
                    "schema_id": case.get("unique_id"),
                    "error": r.error,
                    "latency": r.latency,
                    "ttft": None,
                    "tpot": None,
                    "output_tokens": r.token_count,
                    "parse_ok": False,
                    "schema_ok": False,
                }
            parse_ok, schema_ok, validation_error = validate(r.text, schema)
            return {
                "schema_id": case.get("unique_id"),
                "error": None,
                "latency": r.latency,
                "ttft": r.ttft,
                "tpot": None
                if r.ttft is None or r.token_count <= 1
                else (r.latency - r.ttft) / (r.token_count - 1),
                "output_tokens": r.token_count,
                "parse_ok": parse_ok,
                "schema_ok": schema_ok,
                "validation_error": validation_error,
                "output_preview": r.text[:200],
            }

        if args.closed_loop:
            schedule = closed_loop(len(cases))
            conc = 1
        else:
            schedule = closed_loop(len(cases))
            conc = len(cases)

        result = await run(schedule, send, concurrency=conc)

    summary = _summarize(result.results, result.wall_clock)
    output = {
        "config": {
            "url": url,
            "dataset_id": DATASET_ID,
            "dataset_subset": args.dataset_subset,
            "split": args.split,
            "dataset_revision": args.dataset_revision,
            "dataset_cache_dir": str(args.dataset_cache_dir) if args.dataset_cache_dir else None,
            "limit": args.limit,
            "seed": args.seed,
            "max_tokens": args.max_tokens,
            "closed_loop": args.closed_loop,
        },
        **summary,
        "results": result.results,
    }

    print()
    print("=" * 48)
    print("  MLX 8-bit Llama JSONSchemaBench Results")
    print("=" * 48)
    print(f"Endpoint:          {url}")
    print(f"Schemas:           {len(cases)}")
    print(f"Completed:         {summary['num_completed']}/{summary['num_requests']}")
    print(
        f"Schema valid:      {summary['schema_ok']}/{summary['num_completed']} "
        f"({summary['schema_ok_frac']:.3f})"
    )
    print(f"Token throughput:  {summary['token_throughput']:.2f} tok/s")
    if summary["latency_ms"]:
        print(f"Latency p50:       {summary['latency_ms']['p50']:.1f} ms")
    if summary["ttft_ms"]:
        print(f"TTFT p50:          {summary['ttft_ms']['p50']:.1f} ms")
    if summary["tpot_ms"]:
        print(f"TPOT p50:          {summary['tpot_ms']['p50']:.1f} ms")

    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(output, f, indent=2)
        print(f"Results written to {args.output_json}")
    return output


def _summarize(results: list, wall_clock: float) -> dict:
    successes = [r for r in results if r["error"] is None]
    total_tokens = sum(r["output_tokens"] for r in successes)
    schema_ok = sum(1 for r in successes if r["schema_ok"])
    parse_ok = sum(1 for r in successes if r["parse_ok"])
    return {
        "num_requests": len(results),
        "num_completed": len(successes),
        "num_failed": len(results) - len(successes),
        "actual_duration": wall_clock,
        "request_throughput": len(successes) / wall_clock if wall_clock > 0 else 0,
        "token_throughput": total_tokens / wall_clock if wall_clock > 0 else 0,
        "parse_ok": parse_ok,
        "schema_ok": schema_ok,
        "schema_ok_frac": schema_ok / len(successes) if successes else 0,
        "latency_ms": pct_block([r["latency"] for r in successes], 1000.0),
        "ttft_ms": pct_block([r["ttft"] for r in successes if r["ttft"] is not None], 1000.0),
        "tpot_ms": pct_block([r["tpot"] for r in successes if r["tpot"] is not None], 1000.0),
        "output_tokens": pct_block([r["output_tokens"] for r in successes]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark JSON-schema generation on an MLX 8-bit Llama server."
    )
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--endpoint", default="/v1/completions")
    parser.add_argument("--dataset-subset", default="full")
    parser.add_argument("--split", default="val")
    parser.add_argument("--dataset-revision", default=DATASET_REVISION)
    parser.add_argument("--dataset-cache-dir", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--closed-loop", action="store_true")
    parser.add_argument("--output-json", type=str, default=None)
    args = parser.parse_args()
    asyncio.run(run_benchmark(args))


if __name__ == "__main__":
    main()
