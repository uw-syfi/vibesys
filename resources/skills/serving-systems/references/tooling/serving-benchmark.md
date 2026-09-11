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

**Multi-turn / chat workloads**: model session arrivals open-loop and turns within a session closed-loop (a session sends turn k+1 only after turn k completes, plus think time) — that combination is the faithful model of chat traffic. Pure closed loop with fixed concurrency is a throughput stress test, not a latency instrument: nothing paces the aggregate turn-arrival rate independently of server speed, so a decode-only speedup shortens each session's think-time-to-next-turn cycle and mechanically raises offered load. See the closed-loop pitfall below for a measured case where this flipped a latency verdict.

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

### Node-to-node noise on clusters

On a multi-node job scheduler allocation, p95 TTFT for an equivalent server and workload varies about 30 percent between consecutive runs on the same node, and about 50 percent across nodes of one otherwise-homogeneous partition; some nodes are consistently slow. Example same-node paired runs for trees that later proved equivalent: 718 vs 781 ms, and 1017 vs 799 ms.

Protocol that resolves this to about 10 percent residual noise: run the candidate and the baseline on the same node in the same allocation, three repetitions each against one held server process, and compare the median of p95 across repetitions rather than a single run.

Status: verified. sglang-v0.5.18-rocm700-mi30x, 2026-09-05 to 2026-09-10.

### Repeated reps against one held server are not independent samples

Running the three reps above back-to-back against the *same* server process, with a fixed conversation seed, does not give three independent TTFT samples: reps 2 and 3 hit the prefix-cache entries reps before them installed, even though each rep intends to replay the workload "fresh." Measured p95 TTFT for turn-2+ across three consecutive reps: 899.9 / 772.9 / 598.4 ms, monotonically decreasing (39 percent spread); turn-1 TTFT 412 / 341 / 317 ms, same pattern.

Cause: prefix caching (radix cache) is a property of the server process, not the rep; a fixed seed means every rep replays the same conversations, so rep *N* inherits cache state rep *N-1* installed. See [`../algorithms/radix-prefix-caching.md`](../algorithms/radix-prefix-caching.md) for the mechanism.

Fix, measured: run one warmup rep, then flush the cache (e.g. `POST /flush_cache` while idle) before each measured rep. Over three reps on one node, this cut p95 TTFT run-to-run spread from 39 percent (unflushed) to 8-18 percent, and held TPOT spread at 0.2-0.3 percent; with three reps a same-node paired A/B under this protocol resolves TPOT changes of about 1 percent but p95 TTFT changes only of about 20 percent or more (a measured TTFT delta of -4.85 percent between two builds sat inside the 8-18 percent spread, while their +1.58 percent TPOT delta was clearly resolved); use more reps or a percentile with less tail noise when the target effect on TTFT is smaller. Rep 1 without a flush is the number comparable to a fresh-server evaluation; treat reps taken without an intervening flush as one contaminated sample, not independent repetitions.

Contract: the flush must reset every cache the workload reuses, not only the primary KV cache. SGLang's `/flush_cache` only succeeds while the scheduler is fully idle (it fails and reports the queued/running-request count otherwise), and it resets both the KV radix tree and, on a hybrid attention+SSM model, the Mamba state pool: `Scheduler.flush_cache` (`python/sglang/srt/managers/scheduler.py`) clears `self.tree_cache` and `self.token_to_kv_pool_allocator`, and on a hybrid model `tree_cache` is a `MambaRadixCache` whose `reset()` (`python/sglang/srt/mem_cache/mamba_radix_cache.py`) also drops the per-request SSM state. An endpoint that resets only the KV cache and misses SSM state on a hybrid model would leave stale conversational state across reps; confirm the equivalent contract on other engines before relying on this protocol.

TPOT is not affected by contamination the way TTFT is: even unflushed, it stayed within about 3 percent across three reps (108.0 / 106.6 / 104.7 ms), because it isn't sensitive to prefix-hit position. Prefer TPOT over TTFT when a held-server protocol can't flush between reps.

Scope: any backend and engine with prefix caching enabled (backend-independent); observed on `sglang-v0.5.18-rocm700-mi30x` (aiter attention backend), fix verified on a hybrid (attention + SSM) MoE model. Status: verified (mechanism plus measured). Stamp: sglang-v0.5.18 fork, 2026-09-11, job 632232 (fix measured, 39 percent to 8-18 percent spread); baseline and mechanism from `sglang-v0.5.18-rocm700-mi30x`, 2026-09-10, job 631854.

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
- **First request after boot.** It can trigger lazy kernel compilation and contaminates turn-1 TTFT by an order of magnitude; discard it as warmup, separate from the steady-state warmup window above. Scope: any backend with JIT/lazy kernel builds. Status: verified. sglang-v0.5.18-rocm700-mi30x, 2026-09-05, job 623402.
- **A decode-only speedup regresses p95 TTFT in a closed-loop multi-turn benchmark.** Symptom: a change that only speeds up decode (TPOT down 20 percent, throughput up 22 percent, correctness gates pass) raises p95 TTFT for turn 2+ by 17 percent. Cause: a closed-loop client (each session sends its next turn as soon as the previous answer completes, plus think time) turns a decode speedup into higher offered load, because nothing paces the aggregate turn-arrival rate independently of how fast the server answers: prefill arrivals rose 21.6 percent, co-batched prefills roughly doubled (2.0 to 3.8 percent), and a new turn more often waits behind another session's in-flight prefill. The metric moved because offered load shifted, not because the server got slower. Fix: pace turns on a fixed schedule derived from a reference server speed (send turn k at the later of the scheduled time and the previous completion plus think time), so offered load is independent of the server under test; report throughput next to latency; keep per-turn records so tail attribution is possible. Scope: any engine, backend-independent. Status: verified (mechanism plus measured). Stamp: sglang-v0.5.18 fork, 2026-09-11, job 632238.

## See also

- [`tooling/profiler/`](profiler.md) — when numbers are bad, profile to find why
- [`tooling/fastapi-serving/`](fastapi-serving.md) — endpoint under test
- [`algorithms/radix-prefix-caching/`](../algorithms/radix-prefix-caching.md) — why cache contamination matters
- [`OVERVIEW.md`](../../OVERVIEW.md) — the performance-foundations context benchmarks sit in
