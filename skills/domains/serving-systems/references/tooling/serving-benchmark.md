# Serving benchmark

A correct serving benchmark measures steady-state latency percentiles under a realistic workload and concurrency. The easy mistakes produce results that look great but don't hold up in production.

## Metrics

| Metric | Definition |
|:-------|:-----------|
| **TTFT** (time to first token) | from request submission to first output token at the client |
| **TPOT** (time per output token) | `(E2E_latency − TTFT) / (num_output_tokens − 1)` — average inter-token time after the first |
| **ITL** (inter-token latency) | distribution of per-token gaps — more informative than the average TPOT |
| **E2E latency** | submission → final token |
| **Throughput** | output tokens per second, aggregated across concurrent requests |
| **Goodput** | throughput under SLO constraints (e.g., "throughput where p95 TTFT ≤ 500ms") |

Report percentiles (p50, p95, p99), not means. Means hide tail behavior that matters for SLOs.

## Open-loop vs closed-loop

**Closed-loop**: each client has a fixed concurrency (e.g., 32 workers sending one request at a time, waiting for response, sending next). Equivalent to Little's Law: throughput × avg_latency = concurrency. Results:

- Easy to set up.
- Can **hide tail latency** — if the server slows, the client slows too (back-pressure).
- Good for peak-throughput measurement.

**Open-loop (Poisson arrivals)**: requests arrive at a fixed rate independent of server state. When the server slows, queue builds up, tail latency blows up. Results:

- More production-realistic.
- Exposes SLO-violating behavior closed-loop hides.
- Required for goodput measurement.

**Use open-loop for latency SLOs; use closed-loop for throughput ceiling.** Most benchmarks default to closed-loop because it's easier — know which you're running.

## Warmup and steady state

First N requests are slower due to:
- CUDA / Triton / TorchInductor JIT compile
- Autotune warmup
- CUDA graph capture
- Cold KV cache pool
- Linux file cache for weights

**Warmup**: 30–60 seconds of non-measured load. **Measure** from a later window.

Verify steady state by plotting latency over time — the curve should plateau before measurement starts.

## ISL / OSL distributions

Input-sequence-length and output-sequence-length dominate server behavior. Real workloads have distributions; synthetic benchmarks with fixed ISL=1024, OSL=256 measure one corner of the space.

| Distribution | Source |
|:-------------|:-------|
| **sharegpt** | real conversation traces (ShareGPT export) — widely used, somewhat dated |
| **random ISL/OSL** | sample from uniform or lognormal |
| **real trace replay** | production logs, most realistic, rarely available |
| **fixed** | only appropriate for isolating a specific regime |

Sweep both dimensions; report heatmaps or at least boundary cases (short/short, short/long, long/short, long/long).

## Prefix-cache contamination

Running the same prompt multiple times hits prefix cache; TTFT drops to near zero after the first run. This makes benchmark numbers look great and production numbers not match.

Mitigations:
- Vary prompts per iteration.
- Disable prefix caching for the benchmark run.
- Benchmark cold (fresh model load) if trying to measure without cache.
- **Report whether caching was enabled** — different numbers entirely.

## Tools

| Tool | Strength |
|:-----|:---------|
| **genai-perf** (NVIDIA) | comprehensive, multi-engine, real workload generators |
| **sglang.bench_serving** | open-loop, per-percentile, built into SGLang |
| **vllm bench serve** | closed-loop + open-loop modes |
| **locust** / custom asyncio | DIY — use when you need unusual load patterns |

Don't hand-roll without a reason; these exist for good reasons.

## Example commands

SGLang open-loop benchmark:

```bash
python -m sglang.bench_serving \
    --backend sglang \
    --dataset-name random \
    --random-input-len 1024 --random-output-len 256 \
    --num-prompts 500 \
    --request-rate 4.0 \
    --host localhost --port 30000
```

vLLM benchmark:

```bash
python benchmarks/benchmark_serving.py \
    --model <model> \
    --dataset-name sharegpt \
    --dataset-path ShareGPT_V3_unfiltered_cleaned_split.json \
    --num-prompts 500 \
    --request-rate 4.0
```

## Statistical practices

- **Run N=3+ trials**; report median and spread.
- **Long enough**: 500+ requests at target concurrency, not 50.
- **Fair comparison**: pin hardware, CUDA version, model checkpoint, sampling params.
- **Report version strings**: engine git hash / version, model, precision, kernel backend.

## Reading a benchmark report (skeptically)

Checklist before trusting someone's numbers:

- [ ] Open-loop or closed-loop?
- [ ] Prefix caching enabled?
- [ ] ISL / OSL distribution specified?
- [ ] Warmup excluded from measurement window?
- [ ] Percentiles reported, not just means?
- [ ] Hardware + engine version + precision pinned?
- [ ] Statistical variance / N trials?

If the report misses these, the numbers are suggestive, not authoritative.

## Pitfalls

- **Comparing different backends on different ISL.** The graph looks different at (512, 128) vs (4096, 1024); make sure comparisons hold the workload fixed.
- **Fixed seed across different backends.** Same seed doesn't produce same sampling across engines; don't rely on it for "fairness".
- **Short runs on long models.** Models that take 30s to warm up need benchmarks > 60s.
- **Ignoring network latency.** Client-server on the same box has sub-ms RTT; across a network it's 1–10ms that gets counted in TTFT.
- **Running multiple benchmarks without restart.** Memory-pool state carries over; results drift.
- **Expressing throughput in requests/sec instead of tokens/sec.** Different OSL distributions give different req/s for the same tok/s; always report both.
- **Comparing under-saturated vs saturated.** At low concurrency, server throughput is bounded by request arrivals, not server capacity. Sweep concurrency until saturation.

## See also

- [`tooling/profiler/`](profiler.md) — when numbers are bad, profile to find why
- [`tooling/fastapi-serving/`](fastapi-serving.md) — endpoint under test
- [`algorithms/radix-prefix-caching/`](../algorithms/radix-prefix-caching.md) — why cache contamination matters
- [`OVERVIEW.md`](../../OVERVIEW.md) — the performance-foundations context benchmarks sit in
