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

### Queue wait reads near zero under an overlap scheduler even when a request waits a full step for the device

```
Symptom: server-side request time-stats report queue wait near zero
         (p50 0.6 ms, p95 1.5 ms) while the client-observed TTFT is
         several step times (p50 360 ms), with no server-side field
         that isolates the difference.
Cause:   under an overlap scheduler, the "picked into a batch" timestamp
         is stamped at the top of the scheduler loop, when the CPU-side
         scheduler admits a request into the next batch, before the
         forward is dispatched and before the *previous* batch's results
         are even processed. It marks CPU-side admission, not device
         start; the pick can happen while the prior step's kernels are
         still in flight. The wait behind that in-flight step, the
         request's own forward, and publish/detokenize lag all land
         inside the residual forward-duration field, not in queue wait.
Fix:     decompose with device-timed spans (bracket the forward with a
         profiler window) or compute wait as (first-token publish time
         minus arrival) minus profiled forward duration; do not trust a
         scheduler's pick-time stamp as queue wait under overlap
         scheduling. A second-granularity scheduler log cannot resolve
         this either: it has no per-batch device-duration field.
Scope:   any engine with an overlap/async scheduler (backend-
         independent).
Status:  verified (mechanism read in source, consistent with the
         measured gap). sglang-v0.5.18-rocm700-mi30x, 2026-09-12,
         job-verified.
```

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

**Multi-turn / chat workloads**: model session arrivals open-loop and turns within a session closed-loop (a session sends turn k+1 only after turn k completes, plus think time); that combination is the faithful model of chat traffic. Pure closed loop with fixed concurrency is a throughput stress test, not a latency instrument: nothing paces the aggregate turn-arrival rate independently of server speed, so a decode-only speedup shortens each session's think-time-to-next-turn cycle and mechanically raises offered load. See the closed-loop pitfall below for a measured case where this flipped a latency verdict. State explicitly whether the client keeps the chat template's own generation-prompt form (e.g. an empty think block) in history turns when it rebuilds the next prompt from returned content: some templates render that span differently for the live generation than for history, and a client that reconstructs history from content alone silently caps turn-2+ prefix reuse regardless of server-side cache settings. See [`../algorithms/radix-prefix-caching.md`](../algorithms/radix-prefix-caching.md)'s chat-template pitfall.

A side whose `schedule_bound_fraction` is near 0 has degenerated to closed loop regardless of the pacing mode requested: it is no longer following the fixed schedule, so it measures its own capacity ceiling instead of latency at the offered load. Its deltas against a schedule-bound side (`schedule_bound_fraction` near 1) understate the real gap, because the schedule-bound side's own offered rate is higher. See "Concurrency cap is a workload parameter" below for a measured case (baseline_c48 at `schedule_bound_fraction` 0 versus defaults_c48 at 1.00, offered rates 0.90 vs 1.17 turns/s).

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

### Check whether the client is a term of the metric before trusting server-side latency

The only "server-side" timestamp readily available without extra flags is the scheduler's own `wait_queue_entry_time`, stamped inside the scheduler's busy loop when it polls its request socket (a non-blocking poll drained once per full scheduling iteration). Client TTFT minus that stamp is not the client's own overhead and it is not server-side queue wait either: it also includes however long the request sat unseen between polls, up to one full scheduling iteration, before any server clock recorded it at all (see the overlap-scheduler queue-wait pitfall above for the matching server-side failure mode). Measured against this quantity: p50 54 ms / p95 216 ms at 48 concurrent streams, correlated with requests concurrently mid-prefill (Spearman rho 0.55) more than with prompt length (rho -0.21).

An offline replay of the benchmark client (real per-token pacing, real chunk sizes, 48 concurrent sessions, no server in the loop) measured event-loop wake lag under 1 ms at p95 and under 10 ms worst case: steady-state client-side JSON parsing and event-loop serialization do not reproduce a 216 ms p95, which rules out the client as the source of most of this gap.

Check: do not read client TTFT minus `wait_queue_entry_time` as either "client overhead" or "server queue wait" on its own; both readings mistake a scheduler-poll-granularity artifact for something else. To separate the client's actual contribution, boot with `--enable-metrics` so the tokenizer-manager's own dispatch/receive timestamps propagate into `meta_info`, and take the client-to-tokenizer-manager span as the client's share.

Scope: any benchmark client and any engine with a request-admission busy loop (backend-independent). Status: verified (offline replay measured; field correlation measured). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job-verified.

### Node-to-node noise on clusters

On a multi-node job scheduler allocation, p95 TTFT for an equivalent server and workload varies about 30 percent between consecutive runs on the same node, and about 50 percent across nodes of one otherwise-homogeneous partition; some nodes are consistently slow. Example same-node paired runs for trees that later proved equivalent: 718 vs 781 ms, and 1017 vs 799 ms.

Protocol that resolves this to about 10 percent residual noise: run the candidate and the baseline on the same node in the same allocation, three repetitions each against one held server process, and compare the median of p95 across repetitions rather than a single run.

Status: verified. sglang-v0.5.18-rocm700-mi30x, 2026-09-05 to 2026-09-10.

The cross-node gap can be larger than the same-node figures above: the identical configuration and workload, run on two nodes of the same SKU, gave median TPOT within 2 percent (21.36 vs 21.74 ms) but pooled p95 TTFT turn-2+ 26 percent apart (338 vs 455 ms). Treat this as confirmation that p95 TTFT comparisons must be paired on one node; TPOT is robust across nodes of the same SKU, tail latency is not.

Status: verified. Stamp: sglang-v0.5.18 fork, benchmark_version 4, 2026-09-11, job-verified (cross-node link to the four-side-matrix job).

### Repeated reps against one held server are not independent samples

Running the three reps above back-to-back against the *same* server process, with a fixed conversation seed, does not give three independent TTFT samples: reps 2 and 3 hit the prefix-cache entries reps before them installed, even though each rep intends to replay the workload "fresh." Measured p95 TTFT for turn-2+ across three consecutive reps: 899.9 / 772.9 / 598.4 ms, monotonically decreasing (39 percent spread); turn-1 TTFT 412 / 341 / 317 ms, same pattern.

Cause: prefix caching (radix cache) is a property of the server process, not the rep; a fixed seed means every rep replays the same conversations, so rep *N* inherits cache state rep *N-1* installed. See [`../algorithms/radix-prefix-caching.md`](../algorithms/radix-prefix-caching.md) for the mechanism.

Fix, measured: run one warmup rep, then flush the cache (e.g. `POST /flush_cache` while idle) before each measured rep. Over three reps on one node, this cut p95 TTFT run-to-run spread from 39 percent (unflushed) to 8-18 percent, and held TPOT spread at 0.2-0.3 percent; with three reps a same-node paired A/B under this protocol resolves TPOT changes of about 1 percent but p95 TTFT changes only of about 20 percent or more (a measured TTFT delta of -4.85 percent between two builds sat inside the 8-18 percent spread, while their +1.58 percent TPOT delta was clearly resolved); use more reps or a percentile with less tail noise when the target effect on TTFT is smaller. Rep 1 without a flush is the number comparable to a fresh-server evaluation; treat reps taken without an intervening flush as one contaminated sample, not independent repetitions.

Contract: the flush must reset every cache the workload reuses, not only the primary KV cache. SGLang's `/flush_cache` only succeeds while the scheduler is fully idle (it fails and reports the queued/running-request count otherwise), and it resets both the KV radix tree and, on a hybrid attention+SSM model, the Mamba state pool: `Scheduler.flush_cache` (`python/sglang/srt/managers/scheduler.py`) clears `self.tree_cache` and `self.token_to_kv_pool_allocator`, and on a hybrid model `tree_cache` is a `MambaRadixCache` whose `reset()` (`python/sglang/srt/mem_cache/mamba_radix_cache.py`) also drops the per-request SSM state. An endpoint that resets only the KV cache and misses SSM state on a hybrid model would leave stale conversational state across reps; confirm the equivalent contract on other engines before relying on this protocol.

TPOT is not affected by contamination the way TTFT is: even unflushed, it stayed within about 3 percent across three reps (108.0 / 106.6 / 104.7 ms), because it isn't sensitive to prefix-hit position. Prefer TPOT over TTFT when a held-server protocol can't flush between reps.

Scope: any backend and engine with prefix caching enabled (backend-independent); observed on `sglang-v0.5.18-rocm700-mi30x` (aiter attention backend), fix verified on a hybrid (attention + SSM) MoE model. Status: verified (mechanism plus measured). Stamp: sglang-v0.5.18 fork, 2026-09-11, job-verified (fix measured, 39 percent to 8-18 percent spread); baseline and mechanism from `sglang-v0.5.18-rocm700-mi30x`, 2026-09-10, job-verified.

### Scheduled pacing decouples offered load from server speed

The closed-loop pitfall below shows a decode speedup mechanically raising offered load under pure closed-loop pacing. The fix, sending turn k at the later of a fixed schedule derived from a reference server speed (`REF_TTFT_MS`, `REF_TPOT_MS`) or the previous turn's completion plus think time , was measured against closed-loop pacing on the same held server at the reference speed, then again after two accepted kernel changes raised the server's actual speed:

| Server speed | schedule_bound_fraction (share of sends the schedule, not the server, paced) | scheduled vs. closed |
|:--|:--|:--|
| At the reference speed (TPOT about 107 ms against `REF_TPOT_MS=110`) | 13.5 percent (pooled across 3 reps) | equal: scheduled and closed land within noise of each other, as expected when there is nothing to decouple yet |
| At 1.7x reference throughput (fused MoE + skinny GEMM kernels, TPOT about 36 ms) | 35 to 39 percent | scheduled and closed diverge: TTFT keeps tracking a growing offered rate under scheduled pacing instead of collapsing to the closed-loop coupling this design exists to prevent |

This is the design working as intended: it agrees with closed-loop pacing when nothing has changed, and an increasing share of sends become schedule-bound as the server gets faster, which is what stops a decode speedup from being misread as a pure throughput win with no latency cost. `--pacing closed` reproduces the pre-fix behavior for comparison.

Caveat: p95 TTFT over one rep's roughly 200 turns is noisy regardless of pacing mode (25.7 percent spread across 3 reps under scheduled pacing at the reference speed, 11.2 percent under closed, both 3-rep samples). Use pooled per-turn distributions across reps rather than a single rep's percentile, and prefer TPOT (per-rep spread under 1 percent) when its delta already resolves the comparison.

Scope: any engine, backend-independent (the schedule and the per-rep noise are properties of the benchmark client, not the server under test). Status: verified (mechanism plus measured at two server speeds). Stamp: sglang-v0.5.18 fork, benchmark_version 2, 2026-09-11, job-verified at both the reference-speed run (schedule_bound_fraction 13.5 percent pooled) and the 1.7x-throughput run (schedule_bound_fraction 35 to 39 percent).

### Residual load coupling under fixed-schedule pacing: admission delay is not modeled

Even under the fixed-schedule pacing above, `schedule_bound_fraction` stayed at only 0.35 to 0.39 on a server 1.5 to 1.8x faster than the reference speed the schedule was derived from, well short of the near-1.0 the design intends once the server is faster than the reference. Most turns were still tracking server completion time instead of the schedule, the same failure mode scheduled pacing exists to remove.

Cause: the benchmark client holds a per-session admission-concurrency semaphore (one slot per session, held for the session's whole life), but the fixed schedule sets each session's first-turn send time (`T[s][0]`) as if admission were immediate. With more sessions than slots, the excess sessions actually start 46 to 176 s late; because the schedule assumes no admission wait, their turn-2+ sends fall permanently behind schedule and become completion-bound (tracking server speed) for the rest of the session, 58 to 65 percent of turn-2+ sends in a 166-send rep, in the measured case.

Fix: derive each session's scheduled start time from a simulated admission queue at the reference speed, rather than assuming immediate admission; or make the schedule's first-turn time admission-aware directly. Comparisons made without this fix carry residual load coupling between sides running at different speeds, a faster side's own turns arrive faster, inflating its own tail, which is exactly the effect scheduled pacing was built to remove.

Fixed in benchmark_version 3: the schedule derives each session's start time from a simulated admission queue at the reference speed. Measured on a four-side matrix at different server speeds (the four-side-matrix job): `schedule_bound_fraction` reached 0.99 to 1.00 on every side fast enough to keep up with the schedule, and `offered_turn_rate_per_s` landed within about 3 percent across all four sides regardless of server speed, closing the coupling this pitfall describes.

Scope: any engine, backend-independent (property of the benchmark client's session-admission and scheduling logic, not the server under test). Status: fixed, verified in benchmark_version 3 (mechanism plus measured at two protocol versions). Stamp: sglang-v0.5.18 fork, benchmark_version 2 (bug, job-verified) / benchmark_version 3 (fix, the four-side-matrix job), 2026-09-11.

### Concurrency cap is a workload parameter of the open-loop schedule

The admission-queue simulation above assumes a fixed concurrency cap (16 slots in benchmark_version 3). benchmark_version 4 makes the cap a configurable CLI flag (`--concurrency`, default unlimited, matching production offered load) instead of a hardcoded value; version 3 and version 4 results are comparable only when `--concurrency` matches (version 3's fixed cap corresponds to `--concurrency 16`). Per-token latency under an open-loop schedule is load-dependent: a server fast enough to keep up with the schedule runs smaller batches than one held at the concurrency cap, so its own TPOT reflects that lower batch size, not kernel speed alone. Compare TPOT only between runs at the same offered load and the same concurrency cap.

Scope: any engine, backend-independent. Status: verified (protocol change; 31 unit tests). Stamp: sglang-v0.5.18 fork, benchmark_version 4, 2026-09-11.

### A dispatch threshold goes stale after the kernel it dispatches between changes; the second concurrency is not there to repeat the first win

A dispatch threshold (which of two kernel variants runs at a given batch size or shape) is tuned against the two variants' relative cost at the time it is set. When one variant's load or register profile changes, for example a load-shape fix that speeds up both variants unevenly, the old threshold can go stale silently: it keeps routing every production shape to the now-slower variant with no crossover left to find in the range that matters, so the change measures as a no-op until someone re-sweeps and re-derives the threshold from scratch. Retune any such threshold after every accepted kernel change on either side of its dispatch, not only when a regression is reported. This is also why a paired acceptance run checks a second concurrency rather than repeating the primary one: the second concurrency exists to catch a change that helps one load and hurts another, not to reproduce the first win a second time. When the effect at the primary concurrency clears the acceptance rule and the effect at the secondary concurrency moves the same direction but stays inside that side's own rep-to-rep spread, record it as "no regression at the secondary concurrency," not as a second confirmed win, and do not fold it into the accepted number the primary concurrency already cleared.

Scope: any engine and backend with a batch-size- or shape-keyed kernel dispatch threshold, and any paired acceptance protocol that tests more than one concurrency. Status: verified (mechanism, plus a measured case: a stage1 MoE dispatch threshold tuned at 1024 sorted blocks went stale after a load-shape fix and was retuned to 160; the retune cleared the acceptance rule at 48-session concurrency and showed no regression, but did not clear it at a 16-session cap). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

### Throughput stops differentiating once pacing is open-loop and schedule-bound

Once the schedule binds (`schedule_bound_fraction` near 1.0), throughput converges to the rate the schedule offers, not to the server's own capacity: on a four-side matrix at fixed pacing, throughput varied under 2 percent across sides whose TPOT differed by 5x. Throughput is not a useful differentiator between configurations measured this way; use TPOT and TTFT percentiles instead.

Scope: any engine, backend-independent. Status: verified (measured). Stamp: sglang-v0.5.18 fork, benchmark_version 3, 2026-09-11, job-verified.

### Metric of record: pool per-turn quantiles across reps, not per-rep percentiles

A single rep's server-side stall (a few seconds, affecting a handful of concurrent turns) can dominate that rep's own p95 TTFT while leaving every other rep in the same sample unaffected: one measured case had one rep's p95 at 6944 ms against four reps in the 530 to 627 ms range for the identical configuration. The median of five per-rep p95 values absorbs that stall into "one high rep" and can still read as acceptable; pooling every rep's turn-2+ TTFT into one distribution and taking percentiles across the pool exposes the same event as a heavy p95/p99 tail instead of diluting it.

Use pooled per-turn quantiles (computed across all reps' turns together) as the gating statistic, and report per-rep spread alongside so a single-rep event is visible as a flagged outlier rather than averaged into "high variance."

Scope: any engine, backend-independent. Status: verified (measured). Stamp: sglang-v0.5.18 fork, benchmark_version 2, 2026-09-11, job-verified.

### An unexplained single-rep stall: report pooled quantiles with and without it, gate on the steady-state reps

A reference side can show a one-rep server-side stall with no established cause: the client keeps sending on schedule throughout, the server produces zero scheduler log activity for the stall's duration, then drains a real backlog once it clears. Two measured cases, same signature: an 82 second scheduler blackout in one rep of five (that rep's own p95 TTFT turn2+ at 68.5 s and `schedule_bound_fraction` down to 0.687, against every other rep and the rest of the stalled rep matching steady state), and a shorter 14 second blackout in a separate job's rep 1 (p95 TTFT turn2+ 6111.7 ms and TPOT 51.9 ms for that rep, against 434.9-493.4 ms / 35.8-41.1 ms for the other reps of the same side). Both show the same backlog-burst-drain pattern with no error or retract logged; the underlying cause is still unidentified across both occurrences.

Practice: report both pooled-quantile numbers, across all reps and across the steady-state reps only, and use the steady-state figure as the reference for any accept/reject rule. Do not drop the stalled rep from the record: a rep that still passes its own correctness gate despite a latency stall is real signal, not noise to discard, and averaging it into the accept/reject numbers would hide a real event rather than surface it.

Scope: any engine, backend-independent. Status: observed twice, cause unknown (measured; root cause of the stall itself unresolved in either occurrence). Stamp: sglang-v0.5.18 fork, benchmark_version 4, 2026-09-12, job-verified across the 82-second-stall run and the 14-second-stall run.

### Confirm an environment-variable experiment actually changed behavior before reading the numbers

A variable reaching the server process is not the same as it changing what the process does. After any environment-variable A/B test, grep the boot log for a rejection, a "not supported" warning, or any other line naming the variable, and confirm the setting moved something observable, before comparing the two sides' numbers. A test that only checks `/proc/<pid>/environ` can pass while the two sides run functionally identical configurations; see [`frameworks/pytorch.md`](../frameworks/pytorch.md) for a worked case where this happened with an accepted-but-rejected allocator flag.

Scope: any engine and backend, any env-var-driven A/B test. Status: verified (mechanism plus one measured case). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

### A stochastic failure needs enough reps and a mechanism signal, not just a count

Two byte-identical boots of the same configuration on the same node produced 3 of 5 versus 0 of 5 tail-latency episodes for a failure whose per-repetition rate is around 30 percent: run-to-run variance alone can produce a 3-vs-0 split with no treatment difference at all. At that base rate, 5 reps per side cannot distinguish a real effect from noise; treat a rep count that low as descriptive, not as evidence of a difference between two sides. Before reading a difference in episode count as a finding, use at least 10 reps per side and pair it with an independent mechanism signal (a log line, a counter, a trace) that actually names what changed, not only the outcome count.

Scope: any engine and backend, any comparison of a stochastic per-rep failure or collapse rate. Status: verified (measured: 3/5 vs 0/5 across two identical configurations). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

### Sequential same-node two-boot sweeps can carry a boot-order confound

Staging two configurations as sequential boots on the same node (same job,
one boot after the other, common when a held-server comparison needs only
one config difference) does not guarantee the only thing that differs
between them is the config. In one measured case, two boots differing only
in a TunableOp table (13 tuned buckets added) showed a *larger* single-request
TTFT improvement at prompt lengths entirely outside the tuned range, where
the two configs' tables are byte-identical (-20.3 to -34.5 ms), than at the
tuned lengths the change was supposed to help (-0.6 to -14.7 ms, mostly
smaller than the sweep's own within-target spread). No content difference
between the two boots can explain an improvement at a shape neither side's
table differs on.

Check: before crediting a delta to the config difference under test, compare
the delta at a shape or setting the config change does not touch. If that
untouched-shape delta is comparable in size to the touched-shape delta, the
comparison is confounded by boot order or node state, not measuring the
intended effect; a single sequential pair cannot separate the two. Confirm
with a multi-rep, order-randomized design (alternate which config boots
first, or run more than one pair) before trusting the delta.

Scope: any engine and backend, any two-sided comparison staged as sequential
boots on one node. Status: verified (measured: one config pair's off-target
delta exceeded its on-target delta). Stamp: sglang-v0.5.18-rocm700-mi30x,
2026-09-13, job-verified.

A second, independent case reproduces the same class of confound, this time
from a genuine counterbalanced design rather than a single held-server pair.
A tokenizer-side candidate, suffix-only incremental tokenization (tokenize
only the text after the cached-prefix boundary instead of the whole rendered
conversation), is bit-exact against full re-tokenization on every real turn
shape this harness produces when the cache boundary sits immediately before
a special token (0 of 253 turns mismatched, direct check against the real
checkpoint tokenizer), with a 75.9 percent cache hit rate over a 48-session
round-robin block; the per-turn saving scales with the previous assistant
turn's own length (the new suffix to tokenize), not a flat amount. On the
mechanism alone this candidate measured a client p95 TTFT turn2+ improvement
of 25.9 percent on one held-server pair; replicating that
exact boot order on a different node showed a 1.1 percent regression
instead, flipping the order showed a 14.3 percent improvement (not a clean
reversal, and not the original 25.9 percent either), and a third run at a
lighter concurrency cap showed only a 1.5 percent difference. None of the
three counterbalanced jobs regressed on any metric, and the underlying
tokenizer-stage-level saving this candidate targets (2-6 ms, measured
directly by host stamps, not by end-to-end client timing) is real and
separately confirmed; what did not survive counterbalancing was the larger
end-to-end client-level number the single-pair design produced. Absolute
p95 TTFT at this benchmark's uncapped 48-session concurrency varies enough
boot to boot on its own (one base configuration's own pooled p95 measured
397 ms in one boot and 321-333 ms in another, same code, same environment,
same node type) that a single paired comparison can show a swing this size
by chance alone.

Practical bar from this case: treat a single-pair client p95 TTFT delta
under about 15 percent as indistinguishable from this cluster's own
boot-to-boot noise until confirmed by at least one order-reversed repeat.
A delta that does not hold sign and rough magnitude across both orders is
not yet evidence of a real, order-independent effect, even when every
correctness gate passes cleanly on every rep.

Scope: same as above. Status: verified (three-job counterbalanced design,
one held-server pair per job). Stamp: sglang-v0.5.18-rocm700-mi30x,
2026-09-13, job-verified.

**Follow-on: the combined candidate (this suffix lever plus the send-ids
lever from `../engines/sglang.md`) was accepted as a judgment call, with a
real median-latency improvement.** The
two levers interact (the suffix lever changes what `prompt_ids` is before
the send-ids lever decides whether to forward it as-is), so the combined
path needed its own offline exactness check before any server job: 0 of
253 turns mismatched end to end, matching each lever's own single-lever
check. Three paired server jobs (two c48 boot orders, one c16 cap) then
reran the same counterbalanced design this section already established
was necessary: pooled p50 TTFT turn2+ improved a consistent 3.5 to 4.5
percent (4.0 to 5.7 ms) in every one of the three jobs regardless of boot
order or concurrency, landing inside the pre-registered prediction band,
while the pooled p95 tail delta again proved order-dependent (-0.8 percent
one c48 order, -17.7 percent the reversed order), staying below this
campaign's own detectable delta in both orders. No metric regressed on either
concurrency, and median TPOT was flat to slightly improved everywhere.
Both switches are now on by default for the MI300A platform through the
task's platform config (env defaults, so a caller can still export 0 to
turn either off), while the engine-level defaults stay off. See
[`../engines/sglang.md`](../engines/sglang.md) for the
send-ids lever's own mechanism and numbers.

Scope: same as above. Status: accepted (combined exactness check plus a
three-job counterbalanced design, one held-server pair per job). Stamp:
sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

### A synthetic session driver's own text length can flip which padded bucket a rep lands in

```
Symptom: two reps of a nominally-identical A/B comparison (same nominal
         target token count) show a roughly 10 ms swing in per-forward
         GPU time or TTFT that reproduces even between two boots of the
         exact same configuration on the same side.
Cause:   a session driver that generates word-soup filler text lands the
         real extend-token length within about 10 tokens of the nominal
         target, not exactly on it. When the server pads prefill M to a
         fixed bucket ladder, a handful of tokens either side of a bucket
         edge (e.g. 256 vs 320, 512 vs 576) is enough to land the forward
         in a different bucket, and adjacent buckets can differ enough in
         M to swing GPU time by about 10 ms, with nothing to do with
         whatever the A/B is testing.
Fix:     read each rep's own server-measured extend length (not the
         nominal target) and group or filter by actual landed bucket
         before comparing an A/B delta at a nominal size close to a bucket
         edge, or fix the driver's extend length exactly instead of
         letting it vary.
Scope:   any A/B comparison using a word-soup-based session driver on a
         server that pads to a fixed shape bucket, engine- and
         backend-agnostic.
Status:  verified (reproduced identically across three boots of the exact
         same configuration). Stamp: sglang-v0.5.18-rocm700-mi30x,
         2026-09-13, job-verified.
```

### Decompose TTFT past the forward with cheap host stamps once a profiler's own overhead exceeds the gap

Once a system-level profile has isolated the forward itself, the remaining gap between client TTFT and the forward's own wall time is usually tens of milliseconds, an order profilers with per-op detail (e.g. `record_shapes=True`) are too heavy to measure: that flag alone can add 200-420 ms of CPU overhead per request, several times larger than the gap under investigation, and it cannot be subtracted out because the profiler perturbs exactly the host-dispatch timing the decomposition needs. Bracket each remaining handoff (scheduler pickup, batch build, sample, any draft-extend step, result processing, and upstream of the scheduler: chat-template render, tokenize, IPC dispatch) with plain `time.perf_counter()` stamps behind an env-gated flag instead, and confirm the stamps are monotonic and that their sum reconstructs the measured total end to end before trusting any individual stage. See [`profiler.md`](profiler.md) for the matching CUDA/HIP-event method for the forward's own internal segments.

Scope: any engine, backend-independent (the profiler-overhead-versus-signal-size argument, not the specific stage names). Status: verified (measured: `record_shapes=True` overhead 200-420 ms/request against a 17-33 ms TTFT residual it was meant to explain; a perf_counter-stamped decomposition closed the residual to within 1-2 ms per stage instead). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

## Default-launch validation

A paired acceptance job usually stages its candidate by hand (copying a
tuned artifact into place, exporting an env var on top of an otherwise
unmodified checkout). That proves the mechanism works; it does not prove
that a fresh checkout of the accepted commit, booted the plain way with no
manual staging, actually picks up the same behavior from committed config
alone. Run this check periodically, after every stack change that lands,
not only once:

1. Fresh `git archive` (or equivalent) of the accepted head; no manual
   copy-in-place of any tuned artifact.
2. Boot with harness defaults only: no extra env, no extra argv beyond
   the checked-in recipe.
3. Hard-check every mechanism the accepted stack depends on, each with a
   check that fails the job if missing, not a log grep for a single
   "success" line (a clean load line and a silent failure can coexist;
   see the CRLF pitfall in
   [`../frameworks/pytorch.md`](../frameworks/pytorch.md)):
   - the committed artifact matches the expected shape (line count, row
     count, byte content) at its committed path, not only after staging;
   - the staged, per-rank copies match too;
   - a positive load-verify signal AND a corresponding failure-signal
     check (zero of the known failure mode's own log lines), since
     either alone cannot distinguish "loaded correctly" from "failed
     silently";
   - every warmup or capture step the stack depends on completes at its
     expected count (N of N buckets, N of N ranks);
   - the correctness gate, before and after the measured reps, not only
     around them.
4. One measured rep per concurrency of interest -- this is a wiring
   check, not a re-run of the paired effect-size study -- compared
   against the accepted study's own rep range, not a single point
   estimate; a rep landing near but outside that range is ordinary
   single-rep noise unless it also fails a hard check.
5. Where cheap, run one confirmatory diagnostic beyond the benchmark
   numbers (for example a kernel-name audit against a tuned table) even
   when not required for acceptance: a validation pass is a low-cost
   place to catch a real mechanism divergence that a benchmark number
   alone would only hint at.

Scope: any campaign, any backend. Status: verified (methodology reproduced
across multiple prior instances on this campaign). Stamp:
sglang-v0.5.18-rocm700-mi30x, 2026-09-14, job-verified.

**A default-launch validation surfaced a real, unresolved discrepancy that
a benchmark number alone would have missed.** Re-running a kernel-name
audit (see [`../platforms/`](../platforms/) for the ROCm TunableOp
instance) on a freshly committed head found 345 of 345 dense-GEMM launches
matching a tuned kernel name, the opposite of an earlier job's 0 of 345 on
byte-identical CSV content staged by hand onto an older bundle.
Re-parsing the earlier job's own trace at the corrected M bucket still
shows 0 of 345: the difference is real, not a labeling artifact, and is
not yet explained (candidate causes: the different bundle head, or
committed-path staging via the harness's own loader versus a manual
copy-in-place). Flagged as an open follow-up, not resolved by this
validation pass. Practical lesson: run a validation pass even when
nothing changed on purpose, since "gates pass and the number is in the
right range" does not by itself prove the same kernel path the original
study measured is what is now running.

Status: verified (measured; both jobs' traces cross-checked at the same M
bucket). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-14, job-verified.

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
- **First request after boot.** It can trigger lazy kernel compilation and contaminates turn-1 TTFT by an order of magnitude; discard it as warmup, separate from the steady-state warmup window above. Scope: any backend with JIT/lazy kernel builds. Status: verified. sglang-v0.5.18-rocm700-mi30x, 2026-09-05, job-verified.
- **A decode-only speedup regresses p95 TTFT in a closed-loop multi-turn benchmark.** Symptom: a change that only speeds up decode (TPOT down 20 percent, throughput up 22 percent, correctness gates pass) raises p95 TTFT for turn 2+ by 17 percent. Cause: a closed-loop client (each session sends its next turn as soon as the previous answer completes, plus think time) turns a decode speedup into higher offered load, because nothing paces the aggregate turn-arrival rate independently of how fast the server answers: prefill arrivals rose 21.6 percent, co-batched prefills roughly doubled (2.0 to 3.8 percent), and a new turn more often waits behind another session's in-flight prefill. The metric moved because offered load shifted, not because the server got slower. Fix: pace turns on a fixed schedule derived from a reference server speed (send turn k at the later of the scheduled time and the previous completion plus think time), so offered load is independent of the server under test; report throughput next to latency; keep per-turn records so tail attribution is possible. Scope: any engine, backend-independent. Status: verified (mechanism plus measured). Stamp: sglang-v0.5.18 fork, 2026-09-11, job-verified.
- **Enabling multiple tokenizer-worker processes can turn an occasional multi-second client stall into a routine one, invisibly to server-side tokenizer instrumentation.** Symptom: raising SGLang's tokenizer-worker count (`--tokenizer-worker-num`, a multi-process HTTP mode with a router fanning requests out to several tokenizer worker processes) left every `TokenizerManager`-side timing stamp in its normal few-millisecond range, and left the correctness gate clean on every rep, but pooled client-visible p95 TTFT turn2+ went from 386.7 ms to 9492.6 ms (+2354 percent) at uncapped 48-session concurrency, with individual turns up to 19.5 seconds and a stall count (TTFT over 3000 ms) rising from 3 to 91. Cause: not diagnosed to a specific line; the regression sits at a boundary neither `TokenizerManager`-side timing nor scheduler-side timing stamps, most likely the multi-worker router's own request or response fan-out across worker processes. Because every stamped stage looks normal, a benchmark that only checks `TokenizerManager`-side timings (or the correctness gate) will not catch this: only the end-to-end client-visible TTFT distribution shows it. Fix: do not enable more than one tokenizer worker on this stack without a dedicated investigation instrumenting the router/fan-out boundary itself; if testing it anyway, always compare pooled client-visible tail latency, not just server-side per-stage timings or gate pass/fail. Scope: sglang's Granian multi-worker HTTP mode with `MultiTokenizerRouter` fan-out, any backend; not confirmed whether this is universal to multi-tokenizer-worker mode or specific to this fork's routing implementation. Status: verified (reproduced in one boot at uncapped 48-session concurrency; root cause not yet localized). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

## See also

- [`tooling/profiler/`](profiler.md) — when numbers are bad, profile to find why
- [`tooling/fastapi-serving/`](fastapi-serving.md) — endpoint under test
- [`algorithms/radix-prefix-caching/`](../algorithms/radix-prefix-caching.md) — why cache contamination matters
- [`OVERVIEW.md`](../../OVERVIEW.md) — the performance-foundations context benchmarks sit in
