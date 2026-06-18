"""
Serving benchmark for the Trainium Llama-3-8B server (fixed-length sweep).

Modeled on the in-house Neuron Poisson benchmark: it sweeps a grid of
**fixed input/output token lengths** (default 128 / 256 / 512, input_len ==
output_len per scenario) crossed with **Poisson request rates up to 2.0
req/s**, driving the OpenAI-compatible HTTP server over `/v1/completions`.

For each (length, rate) scenario it fires `--requests-per-scenario` requests
whose arrivals follow a Poisson process, holding the input length fixed
(prompt of exactly `input_len` real tokens) and the output length fixed
(`max_tokens == min_tokens == output_len`, `ignore_eos` so the model emits
the full length where the server honors it). It reports per-scenario TTFT /
TPOT / latency / throughput plus a single headline `aggregate_throughput`
(output tok/s) that the optimization loop tracks across rounds.

Usage:
    python benchmark.py --url http://localhost:8000 \
        --lengths 128,256,512 --rates 0.5,1.0,2.0 \
        --requests-per-scenario 16 --output-json /tmp/bench.json
"""

import argparse
import asyncio
import json
import math
import os
import random
import statistics
import time

import httpx

# A model path is only needed to build realistic fixed-length prompts from
# real token ids. Falls back to a word-repeat approximation if no tokenizer
# is reachable.
_MODEL_PATH_CANDIDATES = ("/model", "/workspace/reference/model", "reference/model")


# ---------------------------------------------------------------------------
# Prompt construction (fixed input length)
# ---------------------------------------------------------------------------


def _load_tokenizer(model_path: str | None):
    """Return (tokenizer, token_pool) or (None, None) if unavailable."""
    candidates = [model_path] if model_path else list(_MODEL_PATH_CANDIDATES)
    candidates = [c for c in candidates if c and os.path.exists(c)]
    if not candidates:
        env = os.environ.get("MODEL_PATH")
        if env and os.path.exists(env):
            candidates = [env]
    for path in candidates:
        try:
            from transformers import AutoTokenizer

            tok = AutoTokenizer.from_pretrained(path)
            excluded = set(getattr(tok, "all_special_ids", []) or [])
            pool = []
            for tid in range(len(tok)):
                if tid in excluded:
                    continue
                piece = tok.decode([tid], skip_special_tokens=True)
                if piece.strip():
                    pool.append(tid)
            if pool:
                print(f"[prompt] tokenizer loaded from {path}; pool={len(pool)} tokens")
                return tok, pool
        except Exception as exc:  # noqa: BLE001 - benchmark must degrade gracefully
            print(f"[prompt] tokenizer load from {path} failed ({exc}); will approximate")
    print("[prompt] no tokenizer available — approximating input length with filler words")
    return None, None


def make_prompt(tokenizer, token_pool, input_len: int, rng: random.Random) -> str:
    """Build a prompt of (approximately) ``input_len`` tokens."""
    if tokenizer is not None and token_pool:
        ids = [rng.choice(token_pool) for _ in range(input_len)]
        return tokenizer.decode(ids, skip_special_tokens=True)
    # Fallback: one filler word ~ one token.
    return " ".join(["token"] * input_len)


# ---------------------------------------------------------------------------
# Per-request measurement
# ---------------------------------------------------------------------------


async def send_request(
    client: httpx.AsyncClient,
    url: str,
    prompt: str,
    output_len: int,
    temperature: float,
) -> dict:
    """Stream one fixed-output-length completion and measure timings."""
    body = {
        "prompt": prompt,
        "max_tokens": output_len,
        # Best-effort exact output length; servers that don't support these
        # extra fields simply stop at EOS and we record the actual count.
        "min_tokens": output_len,
        "ignore_eos": True,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    t_send = time.perf_counter()
    t_first = None
    t_done = None
    output_tokens = 0
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
                    t_done = time.perf_counter()
                    break
                chunk = json.loads(payload)
                usage = chunk.get("usage")
                if usage and usage.get("completion_tokens") is not None:
                    usage_tokens = usage["completion_tokens"]
                choices = chunk.get("choices") or []
                if choices:
                    text = choices[0].get("text") or ""
                    if text:
                        output_tokens += 1
                        if t_first is None:
                            t_first = time.perf_counter()
    except Exception as exc:  # noqa: BLE001 - record, don't crash the sweep
        error = str(exc)

    if t_done is None:
        t_done = time.perf_counter()
    # Prefer server-reported usage when present (more accurate than chunk count).
    gen_tokens = usage_tokens if usage_tokens is not None else output_tokens

    ttft = (t_first - t_send) if t_first is not None else None
    tpot = None
    if t_first is not None and gen_tokens > 1:
        tpot = (t_done - t_first) / (gen_tokens - 1)
    return {
        "error": error,
        "generated_tokens": gen_tokens,
        "ttft_s": ttft,
        "tpot_s": tpot,
        "latency_s": t_done - t_send,
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = math.ceil((pct / 100.0) * len(s)) - 1
    return s[max(0, min(idx, len(s) - 1))]


def _stat_block(values: list[float]) -> dict | None:
    if not values:
        return None
    return {
        "mean": statistics.fmean(values),
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "p99": percentile(values, 99),
    }


def summarize(results: list[dict], wall_s: float) -> dict:
    ok = [r for r in results if r["error"] is None]
    ttfts = [r["ttft_s"] for r in ok if r["ttft_s"] is not None]
    tpots = [r["tpot_s"] for r in ok if r["tpot_s"] is not None]
    latencies = [r["latency_s"] for r in ok]
    gen = sum(r["generated_tokens"] for r in ok)
    return {
        "requests": len(results),
        "completed": len(ok),
        "failed": len(results) - len(ok),
        "generated_tokens": gen,
        "wall_s": wall_s,
        "aggregate_output_tokens_per_s": gen / wall_s if wall_s > 0 else None,
        "request_throughput_per_s": len(ok) / wall_s if wall_s > 0 else None,
        "ttft_s": _stat_block(ttfts),
        "tpot_s": _stat_block(tpots),
        "latency_s": _stat_block(latencies),
    }


# ---------------------------------------------------------------------------
# Scenario driver (Poisson arrivals, fixed in/out length)
# ---------------------------------------------------------------------------


async def run_scenario(
    client: httpx.AsyncClient,
    url: str,
    tokenizer,
    token_pool,
    input_len: int,
    output_len: int,
    rate: float,
    requests: int,
    temperature: float,
    seed: int,
) -> dict:
    rng = random.Random(seed)
    run_start = time.perf_counter()
    scheduled = run_start
    tasks: list[asyncio.Task] = []
    for index in range(requests):
        if index:
            scheduled += rng.expovariate(rate)
        delay = scheduled - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        prompt = make_prompt(tokenizer, token_pool, input_len, rng)
        tasks.append(
            asyncio.create_task(send_request(client, url, prompt, output_len, temperature))
        )
    results = await asyncio.gather(*tasks)
    wall_s = time.perf_counter() - run_start
    summary = summarize(results, wall_s)
    summary.update(
        {
            "input_len": input_len,
            "output_len": output_len,
            "request_rate_per_s": rate,
            "requested_requests": requests,
            "seed": seed,
        }
    )
    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run_benchmark(args: argparse.Namespace) -> dict:
    url = args.url.rstrip("/") + args.endpoint
    lengths = [int(v) for v in args.lengths.split(",") if v.strip()]
    rates = [float(v) for v in args.rates.split(",") if v.strip()]
    tokenizer, token_pool = _load_tokenizer(args.model_path)

    print(
        json.dumps(
            {"url": url, "lengths": lengths, "rates": rates,
             "requests_per_scenario": args.requests_per_scenario},
            indent=2,
        ),
        flush=True,
    )

    scenarios: list[dict] = []
    total_tokens = 0
    total_wall = 0.0
    total_completed = 0
    total_failed = 0
    peak = 0.0
    seed = args.seed

    async with httpx.AsyncClient() as client:
        for length in lengths:
            for rate in rates:
                print(f"SCENARIO input={length} output={length} rate={rate}", flush=True)
                summary = await run_scenario(
                    client=client,
                    url=url,
                    tokenizer=tokenizer,
                    token_pool=token_pool,
                    input_len=length,
                    output_len=length,
                    rate=rate,
                    requests=args.requests_per_scenario,
                    temperature=args.temperature,
                    seed=seed,
                )
                seed += 1
                scenarios.append(summary)
                total_tokens += summary["generated_tokens"]
                total_wall += summary["wall_s"]
                total_completed += summary["completed"]
                total_failed += summary["failed"]
                tput = summary["aggregate_output_tokens_per_s"] or 0.0
                peak = max(peak, tput)
                print(json.dumps(summary, indent=2), flush=True)

    # Headline throughput tracked by the optimization loop: total output
    # tokens over total wall-clock across the whole sweep. Stable, single
    # scalar — keep this field name (`aggregate_throughput`).
    aggregate_throughput = total_tokens / total_wall if total_wall > 0 else 0.0

    result = {
        "config": {
            "url": url,
            "lengths": lengths,
            "rates": rates,
            "requests_per_scenario": args.requests_per_scenario,
            "temperature": args.temperature,
            "seed": args.seed,
        },
        "aggregate_throughput": aggregate_throughput,
        "peak_aggregate_throughput": peak,
        "total_tokens": total_tokens,
        "actual_duration": total_wall,
        "num_completed": total_completed,
        "num_failed": total_failed,
        "scenarios": scenarios,
    }

    print()
    print("=" * 48)
    print("  Benchmark summary (fixed-length Poisson sweep)")
    print("=" * 48)
    print(f"Scenarios:            {len(scenarios)} ({len(lengths)} lengths x {len(rates)} rates)")
    print(f"Completed / failed:   {total_completed} / {total_failed}")
    print(f"Aggregate throughput: {aggregate_throughput:.1f} tok/s  (headline)")
    print(f"Peak scenario tput:   {peak:.1f} tok/s")

    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Results written to {args.output_json}")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fixed-length Poisson benchmark for an OpenAI-compatible server."
    )
    parser.add_argument("--url", default="http://localhost:8000", help="Server base URL")
    parser.add_argument("--endpoint", default="/v1/completions", help="API endpoint path")
    parser.add_argument(
        "--lengths", default="128,256,512",
        help="Comma-separated fixed in/out token lengths (input_len == output_len).",
    )
    parser.add_argument(
        "--rates", default="0.5,1.0,2.0",
        help="Comma-separated Poisson request rates (req/s), up to ~2.0.",
    )
    parser.add_argument("--requests-per-scenario", type=int, default=16)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--model-path", default=None,
        help="Path to model dir for tokenizer (default: /model, then "
             "/workspace/reference/model). Falls back to filler words.",
    )
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument(
        "--output-json", default=None,
        help="If set, write structured results (incl. aggregate_throughput) here.",
    )
    args = parser.parse_args()
    asyncio.run(run_benchmark(args))


if __name__ == "__main__":
    main()
