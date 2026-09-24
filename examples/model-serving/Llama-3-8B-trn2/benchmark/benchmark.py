"""
Serving benchmark for the Trainium Llama-3-8B server (warm, closed-loop).
Warm-up before timing to exclude neuronx-cc compiles. Closed-loop concurrency
sweep over (length, concurrency) pairs. The headline `aggregate_throughput` is
the peak steady-state output tok/s across the sweep.
Usage:
    python benchmark.py --url http://localhost:8000 \
        --lengths 128,256,512 --concurrency 1,2,4,8 \
        --duration 20 --output-json /tmp/bench.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics

import httpx

from vs_bench.runner import run
from vs_bench.schedule import duration_limited
from vs_bench.stats import pct_block
from vs_bench.transport import stream_sse

_MODEL_PATH_CANDIDATES = ("/model", "/workspace/reference/model", "reference/model")


def _load_tokenizer(model_path):
    candidates = [model_path] if model_path else list(_MODEL_PATH_CANDIDATES)
    candidates = [c for c in candidates if c and os.path.exists(c)]
    if not candidates and os.environ.get("MODEL_PATH") and os.path.exists(os.environ["MODEL_PATH"]):
        candidates = [os.environ["MODEL_PATH"]]
    for path in candidates:
        try:
            from transformers import AutoTokenizer

            tok = AutoTokenizer.from_pretrained(path)
            excluded = set(getattr(tok, "all_special_ids", []) or [])
            pool = [
                t
                for t in range(len(tok))
                if t not in excluded and tok.decode([t], skip_special_tokens=True).strip()
            ]
            if pool:
                print(f"[prompt] tokenizer from {path}; pool={len(pool)}")
                return tok, pool
        except Exception as exc:
            print(f"[prompt] tokenizer load from {path} failed ({exc})")
    print("[prompt] no tokenizer — approximating input length with filler words")
    return None, None


def make_prompt(tokenizer, pool, input_len, rng):
    if tokenizer is not None and pool:
        return tokenizer.decode(
            [rng.choice(pool) for _ in range(input_len)], skip_special_tokens=True
        )
    return " ".join(["token"] * input_len)


async def measure(client, url, tokenizer, pool, length, concurrency, duration, temperature, rng):
    async def send(i: int) -> dict:
        prompt = make_prompt(tokenizer, pool, length, rng)
        r = await stream_sse(
            client,
            url,
            {
                "prompt": prompt,
                "max_tokens": length,
                "min_tokens": length,
                "ignore_eos": True,
                "temperature": temperature,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
            timeout=600.0,
        )
        gen = (
            r.usage["completion_tokens"]
            if r.usage and "completion_tokens" in r.usage
            else r.token_count
        )
        ttft = r.ttft
        tpot = (r.latency - ttft) / (gen - 1) if (ttft is not None and gen > 1) else None
        return {
            "error": r.error,
            "gen_tokens": gen,
            "ttft_s": ttft,
            "tpot_s": tpot,
            "latency_s": r.latency,
        }

    result = await run(duration_limited(duration), send, concurrency=concurrency)
    ok = [r for r in result.results if r["error"] is None]
    gen = sum(r["gen_tokens"] for r in ok)
    wall = result.wall_clock
    ttfts = [r["ttft_s"] for r in ok if r["ttft_s"] is not None]
    tpots = [r["tpot_s"] for r in ok if r["tpot_s"] is not None]
    return {
        "input_len": length,
        "output_len": length,
        "concurrency": concurrency,
        "completed": len(ok),
        "failed": len(result.results) - len(ok),
        "wall_s": wall,
        "generated_tokens": gen,
        "output_tokens_per_s": gen / wall if wall > 0 else 0.0,
        "request_throughput_per_s": len(ok) / wall if wall > 0 else 0.0,
        "ttft_s": {
            "p50": pct_block(ttfts)["p50"] if ttfts else None,
            "p90": pct_block(ttfts)["p90"] if ttfts else None,
            "mean": statistics.fmean(ttfts) if ttfts else None,
        },
        "tpot_s": {
            "p50": pct_block(tpots)["p50"] if tpots else None,
            "p90": pct_block(tpots)["p90"] if tpots else None,
            "mean": statistics.fmean(tpots) if tpots else None,
        },
    }


async def run_benchmark(args):
    url = args.url.rstrip("/") + args.endpoint
    lengths = [int(v) for v in args.lengths.split(",") if v.strip()]
    concs = [int(v) for v in args.concurrency.split(",") if v.strip()]
    tokenizer, pool = _load_tokenizer(args.model_path)
    rng = random.Random(args.seed)
    print(
        json.dumps(
            {
                "url": url,
                "lengths": lengths,
                "concurrency": concs,
                "duration_s": args.duration,
                "warmup_requests": args.warmup_requests,
            },
            indent=2,
        ),
        flush=True,
    )
    scenarios: list[dict] = []
    async with httpx.AsyncClient() as client:
        # Warmup: compile every length bucket
        if args.warmup_requests > 0:
            print("Warming up (compiling buckets; not timed) ...", flush=True)
            warm = []
            for length in lengths:
                for _ in range(args.warmup_requests):
                    warm.append(
                        stream_sse(
                            client,
                            url,
                            {
                                "prompt": make_prompt(tokenizer, pool, length, rng),
                                "max_tokens": length,
                                "min_tokens": length,
                                "ignore_eos": True,
                                "temperature": args.temperature,
                                "stream": True,
                            },
                            timeout=600.0,
                        )
                    )
            await asyncio.gather(*warm)
            print("Warm-up done.\n", flush=True)
        # Timed closed-loop sweep
        for length in lengths:
            for c in concs:
                print(
                    f"MEASURE length={length} concurrency={c} for {args.duration}s ...", flush=True
                )
                s = await measure(
                    client, url, tokenizer, pool, length, c, args.duration, args.temperature, rng
                )
                scenarios.append(s)
                print(
                    f"  -> {s['output_tokens_per_s']:.1f} tok/s "
                    f"({s['completed']} reqs, tpot_p50={s['tpot_s']['p50']})",
                    flush=True,
                )
    peak = max((s["output_tokens_per_s"] for s in scenarios), default=0.0)
    best = max(scenarios, key=lambda s: s["output_tokens_per_s"], default=None)
    result = {
        "config": {
            "url": url,
            "lengths": lengths,
            "concurrency": concs,
            "duration_s": args.duration,
            "warmup_requests": args.warmup_requests,
            "temperature": args.temperature,
            "seed": args.seed,
        },
        "aggregate_throughput": peak,
        "peak_scenario": {k: best[k] for k in ("input_len", "concurrency", "output_tokens_per_s")}
        if best
        else None,
        "scenarios": scenarios,
    }
    print("\n" + "=" * 56)
    print("  Benchmark summary (warm, closed-loop)")
    print("=" * 56)
    for s in scenarios:
        print(
            f"  len={s['input_len']:>4} c={s['concurrency']:>2}  "
            f"{s['output_tokens_per_s']:7.1f} tok/s   req/s={s['request_throughput_per_s']:.2f}"
        )
    print(f"\nAggregate throughput (peak steady-state): {peak:.1f} tok/s  (headline)")
    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Results written to {args.output_json}")
    return result


def main():
    p = argparse.ArgumentParser(
        description="Warm, closed-loop benchmark for an OpenAI-compatible server."
    )
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--endpoint", default="/v1/completions")
    p.add_argument("--lengths", default="128,256,512")
    p.add_argument("--concurrency", default="1,2,4,8")
    p.add_argument("--duration", type=float, default=20.0)
    p.add_argument("--warmup-requests", type=int, default=2)
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--model-path", default=None)
    p.add_argument("--seed", type=int, default=1234)
    p.add_argument("--output-json", default=None)
    args = p.parse_args()
    asyncio.run(run_benchmark(args))


if __name__ == "__main__":
    main()
