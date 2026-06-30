"""
Serving benchmark for the Trainium Llama-3-8B server (warm, closed-loop).

Two deliberate design choices so the headline number reflects *real serving
throughput*, not artifacts:

1. **Warm-up before timing.** A first request to a new shape bucket triggers a
   multi-minute `neuronx-cc` compile. Those compiles are run once up front and
   excluded from the measurement, so the metric is steady-state, not compile-
   dominated.

2. **Closed-loop concurrency (no open-loop overload).** Instead of offering a
   fixed Poisson rate the server may not be able to meet (which turns the metric
   into a queue-wait measurement), we keep exactly C requests in flight at all
   times for a fixed duration and sweep C. This self-paces to the server's
   capacity and rewards real device utilization: higher C drives bigger decode
   batches until the NeuronCore saturates.

Fixed input/output token lengths (default 128/256/512). The headline
`aggregate_throughput` is the **peak steady-state output tok/s** across the
(length, concurrency) sweep — the max throughput the server can actually sustain.

Usage:
    python benchmark.py --url http://localhost:8000 \
        --lengths 128,256,512 --concurrency 1,2,4,8 \
        --duration 20 --output-json /tmp/bench.json
"""

import argparse
import asyncio
import json
import os
import random
import statistics
import time

import httpx

_MODEL_PATH_CANDIDATES = ("/model", "/workspace/reference/model", "reference/model")


# ---------------------------------------------------------------------------
# Prompt construction (fixed input length)
# ---------------------------------------------------------------------------


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
            pool = [t for t in range(len(tok))
                    if t not in excluded and tok.decode([t], skip_special_tokens=True).strip()]
            if pool:
                print(f"[prompt] tokenizer from {path}; pool={len(pool)}")
                return tok, pool
        except Exception as exc:  # noqa: BLE001
            print(f"[prompt] tokenizer load from {path} failed ({exc})")
    print("[prompt] no tokenizer — approximating input length with filler words")
    return None, None


def make_prompt(tokenizer, pool, input_len, rng):
    if tokenizer is not None and pool:
        return tokenizer.decode([rng.choice(pool) for _ in range(input_len)], skip_special_tokens=True)
    return " ".join(["token"] * input_len)


# ---------------------------------------------------------------------------
# One request (streaming, fixed output length)
# ---------------------------------------------------------------------------


async def send_request(client, url, prompt, output_len, temperature):
    body = {
        "prompt": prompt,
        "max_tokens": output_len,
        "min_tokens": output_len,
        "ignore_eos": True,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t_send = time.perf_counter()
    t_first = None
    out_tokens = 0
    usage_tokens = None
    error = None
    try:
        async with client.stream("POST", url, json=body, timeout=600.0) as resp:
            resp.raise_for_status()
            async for raw in resp.aiter_lines():
                if not raw.startswith("data: "):
                    continue
                payload = raw[len("data: "):]
                if payload.strip() == "[DONE]":
                    break
                chunk = json.loads(payload)
                usage = chunk.get("usage")
                if usage and usage.get("completion_tokens") is not None:
                    usage_tokens = usage["completion_tokens"]
                choices = chunk.get("choices") or []
                if choices and (choices[0].get("text") or ""):
                    out_tokens += 1
                    if t_first is None:
                        t_first = time.perf_counter()
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    t_done = time.perf_counter()
    gen = usage_tokens if usage_tokens is not None else out_tokens
    ttft = (t_first - t_send) if t_first is not None else None
    tpot = (t_done - t_first) / (gen - 1) if (t_first is not None and gen > 1) else None
    return {"error": error, "gen_tokens": gen, "ttft_s": ttft, "tpot_s": tpot, "latency_s": t_done - t_send}


# ---------------------------------------------------------------------------
# Closed-loop measurement: keep C requests in flight for `duration` seconds
# ---------------------------------------------------------------------------


def _pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    import math
    return s[max(0, min(int(math.ceil(p / 100 * len(s))) - 1, len(s) - 1))]


async def measure(client, url, tokenizer, pool, length, concurrency, duration, temperature, rng):
    deadline = time.perf_counter() + duration
    results: list[dict] = []

    async def worker():
        while time.perf_counter() < deadline:
            prompt = make_prompt(tokenizer, pool, length, rng)
            results.append(await send_request(client, url, prompt, length, temperature))

    t0 = time.perf_counter()
    await asyncio.gather(*[worker() for _ in range(concurrency)])
    wall = time.perf_counter() - t0

    ok = [r for r in results if r["error"] is None]
    gen = sum(r["gen_tokens"] for r in ok)
    ttfts = [r["ttft_s"] for r in ok if r["ttft_s"] is not None]
    tpots = [r["tpot_s"] for r in ok if r["tpot_s"] is not None]
    return {
        "input_len": length, "output_len": length, "concurrency": concurrency,
        "completed": len(ok), "failed": len(results) - len(ok),
        "wall_s": wall, "generated_tokens": gen,
        "output_tokens_per_s": gen / wall if wall > 0 else 0.0,
        "request_throughput_per_s": len(ok) / wall if wall > 0 else 0.0,
        "ttft_s": {"p50": _pct(ttfts, 50), "p90": _pct(ttfts, 90),
                   "mean": statistics.fmean(ttfts) if ttfts else None},
        "tpot_s": {"p50": _pct(tpots, 50), "p90": _pct(tpots, 90),
                   "mean": statistics.fmean(tpots) if tpots else None},
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run_benchmark(args):
    url = args.url.rstrip("/") + args.endpoint
    lengths = [int(v) for v in args.lengths.split(",") if v.strip()]
    concs = [int(v) for v in args.concurrency.split(",") if v.strip()]
    tokenizer, pool = _load_tokenizer(args.model_path)
    rng = random.Random(args.seed)

    print(json.dumps({"url": url, "lengths": lengths, "concurrency": concs,
                      "duration_s": args.duration, "warmup_requests": args.warmup_requests}, indent=2),
          flush=True)

    scenarios: list[dict] = []
    async with httpx.AsyncClient() as client:
        # --- Warm-up: compile every length bucket, untimed and discarded. ---
        if args.warmup_requests > 0:
            print("Warming up (compiling buckets; not timed) ...", flush=True)
            warm = []
            for length in lengths:
                for _ in range(args.warmup_requests):
                    warm.append(send_request(client, url,
                                             make_prompt(tokenizer, pool, length, rng),
                                             length, args.temperature))
            await asyncio.gather(*warm)
            print("Warm-up done.\n", flush=True)

        # --- Timed closed-loop sweep over (length, concurrency). ---
        for length in lengths:
            for c in concs:
                print(f"MEASURE length={length} concurrency={c} for {args.duration}s ...", flush=True)
                s = await measure(client, url, tokenizer, pool, length, c,
                                  args.duration, args.temperature, rng)
                scenarios.append(s)
                print(f"  -> {s['output_tokens_per_s']:.1f} tok/s "
                      f"({s['completed']} reqs, tpot_p50={s['tpot_s']['p50']})", flush=True)

    # Headline: peak sustained output tok/s across the sweep (warm, no overload).
    peak = max((s["output_tokens_per_s"] for s in scenarios), default=0.0)
    best = max(scenarios, key=lambda s: s["output_tokens_per_s"], default=None)

    result = {
        "config": {"url": url, "lengths": lengths, "concurrency": concs,
                   "duration_s": args.duration, "warmup_requests": args.warmup_requests,
                   "temperature": args.temperature, "seed": args.seed},
        "aggregate_throughput": peak,            # headline: peak steady-state tok/s
        "peak_scenario": {k: best[k] for k in ("input_len", "concurrency",
                          "output_tokens_per_s")} if best else None,
        "scenarios": scenarios,
    }

    print("\n" + "=" * 56)
    print("  Benchmark summary (warm, closed-loop)")
    print("=" * 56)
    for s in scenarios:
        print(f"  len={s['input_len']:>4} c={s['concurrency']:>2}  "
              f"{s['output_tokens_per_s']:7.1f} tok/s   req/s={s['request_throughput_per_s']:.2f}")
    print(f"\nAggregate throughput (peak steady-state): {peak:.1f} tok/s  (headline)")

    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Results written to {args.output_json}")
    return result


def main():
    p = argparse.ArgumentParser(description="Warm, closed-loop benchmark for an OpenAI-compatible server.")
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--endpoint", default="/v1/completions")
    p.add_argument("--lengths", default="128,256,512",
                   help="Comma-separated fixed in/out token lengths (input_len == output_len).")
    p.add_argument("--concurrency", default="1,2,4,8",
                   help="Comma-separated closed-loop concurrency levels to sweep.")
    p.add_argument("--duration", type=float, default=20.0,
                   help="Seconds to hold each (length, concurrency) measurement.")
    p.add_argument("--warmup-requests", type=int, default=2,
                   help="Untimed requests per length to compile buckets before timing.")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--model-path", default=None)
    p.add_argument("--seed", type=int, default=1234)
    p.add_argument("--output-json", default=None,
                   help="Write structured results (incl. aggregate_throughput) here.")
    args = p.parse_args()
    asyncio.run(run_benchmark(args))


if __name__ == "__main__":
    main()
