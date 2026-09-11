---
name: optimization-loop
description: Run a performance-optimization campaign on one serving metric and one hardware target. Use when asked to improve latency, throughput, TTFT, TPOT, or a step time of an inference server, or to drive an optimization effort by hand outside VibeSys. Covers fixing the measurement and noise floor, bounding the step with a roofline, decomposing with a profile, hypotheses with predicted numbers, testing at the cheapest level that discriminates, paired one-variable comparison, correctness gates on every run, the experiment ledger, re-profiling, and stop criteria.
---

# Optimization loop

The end-to-end workflow for improving one serving metric on one hardware target. It sequences the serving-systems references under `resources/skills/serving-systems/references/tooling/` rather than repeating them:

- [serving-benchmark.md](../../../resources/skills/serving-systems/references/tooling/serving-benchmark.md) for metric definitions, warmup, and statistical practice.
- [performance-modeling.md](../../../resources/skills/serving-systems/references/tooling/performance-modeling.md) for the roofline, Amdahl bounds, and the plateau workflow.
- [profiler.md](../../../resources/skills/serving-systems/references/tooling/profiler.md) for profiling altitudes and the classification-to-fix table.
- [accuracy-checker.md](../../../resources/skills/serving-systems/references/tooling/accuracy-checker.md) for the correctness gate.
- `resources/skills/serving-systems/references/platforms/<backend>/` for the floor, hardware numbers, and profiler toolchain of the target.

Findings from a campaign go into the ledger (below) and are promoted into the serving-systems knowledge tree with the `curate-serving-knowledge` skill. This skill never edits the tree directly.

## Prerequisites

- One objective metric with a direction (for example p95 TTFT for turn 2+, lower is better) and a correctness gate that runs alongside it.
- A reproducible benchmark: fixed seed, fixed input and output lengths, fixed concurrency.
- Access to the platform profiler and the hardware peak numbers.
- The task's site and platform config (paths, launch recipe) checked in next to the harness.

## The loop

| Step | Output |
|:--|:--|
| 0. Read the knowledge tree | Known floor, pitfalls, and prior results for this platform and model |
| 1. Fix the measurement | Noise floor, comparison protocol, detectable delta |
| 2. Compute the bound | Roofline per stage, stop criterion |
| 3. Decompose | Time per stage, gap attributed |
| 4. Write hypotheses | Predicted number per hypothesis, tree checked for prior answers |
| 5. Test at the cheapest level | Microbenchmark, held server, or full round |
| 6. Change one variable, compare paired | Accept or reject |
| 7. Re-check correctness | Gate pass or fail |
| 8. Record | Ledger row |
| 9. Re-profile and repeat | New top term, back to step 3 |

Steps 0 to 2 happen once per design; steps 3 to 9 are the iteration.

## 0. Read the knowledge tree first

Before the first boot, read in full for the target backend: `platforms/<backend>/floor.md`, `hardware.md`, and the model's file under `references/models/` if one exists. This is where prior campaigns' blockers get skipped. Treat entries whose verified-on stamp predates the current image or engine version as hypotheses to re-verify, not facts.

Two later read points:

- At each new hypothesis, open the one topic file that matches it and check whether the tree already answers the question.
- On an unfamiliar failure, grep the tree for the error string before debugging. Pitfall entries are written symptom-first for this reason.

## 1. Fix the measurement first

No experiment is interpretable until the protocol can detect the smallest change worth shipping. Establish, before any code change:

- **Noise floor.** Run the benchmark repeatedly against one server on one node. Then across boots on the same node. Then across nodes. Record all three spreads. Multi-node clusters commonly show 30 to 50 percent p95 variance across nodes from cache state, thermal state, and neighbors on shared storage or fabric.
- **Comparison protocol.** Whatever the noise floor dictates. The default that survives most clusters: candidate and baseline on the same node in the same allocation, back to back, three benchmark repetitions each against one held server, compare medians of the percentile. Reboot only when the change requires it.
- **Detectable delta.** State it: "this protocol resolves a 10 percent change". If the target improvement is below the detectable delta, fix the protocol, not the code.
- **Time per sample.** Server boot dominates; reduce it before iterating (staged checkouts, pre-sharded weights, warm kernel caches). Every minute of boot is paid on every sample of every experiment.
- **Load-model coupling.** Before the first candidate, check whether the load model couples offered load to the quantity being optimized (a closed-loop client couples offered load to decode speed) — otherwise a real improvement in one metric can read as a regression in another.

## 2. Compute the bound

Follow performance-modeling.md to build the roofline for the step under test: bytes read over memory bandwidth, FLOPs over peak, bytes over interconnect for collectives. Write down the bound and the measured number side by side. The ratio decides the campaign shape:

| Measured over bound | Meaning | Approach |
|:--|:--|:--|
| Above 3x | A stage is structurally wrong: wrong kernel, wrong path, missing cache hit | Find the stage, replace it |
| 1.3x to 3x | Tuning and overhead | Kernel configs, launch overhead, scheduler |
| Under 1.3x | At the floor for this design | Stop, or change the design and recompute |

The bound is a function of the design, not only of the hardware. Recompute it when a change alters what a step reads, computes, or communicates:

- Batch composition changes weight bytes per step (MoE: experts touched per layer scales with tokens times top-k until it saturates).
- A parallelism change (tensor to expert parallel) changes per-device bytes and swaps collective types.
- A quantization path change (in-kernel dequant versus a dequantized scratch buffer) changes bytes by the format ratio.
- A cache policy change alters how many tokens a prefill computes.

Do not recompute for tuning inside a fixed design (tile sizes, launch parameters); the target there is the existing bound.

The first bound is a single term and is unreachable by construction. After the first profile, refine it into a per-stage budget (kernel at its bound, collectives at their latency floor, launch overhead). The refined bound is the stop criterion.

## 3. Decompose

Split the metric into stages with the profiler before guessing. For a serving latency metric the stages are usually:

- Client to server overhead: tokenization, template rendering, transport, detokenization.
- Queue wait: time until the scheduler admits the request, including waiting on an in-progress step.
- Cache outcome: tokens computed versus tokens reused from the prefix cache.
- Compute: per-kernel time in one step, attributed to model blocks.
- Collectives: time in cross-device synchronization.

Attribute the gap from step 2 to these stages. One profile of one step is usually enough to name the top term; a full-trace profile of the benchmark is rarely needed and often too heavy to run.

## 4. Write hypotheses with a predicted number

Each hypothesis states a mechanism and a number the experiment will produce if the mechanism is real:

```
H: the MoE grouped GEMM accounts for 80 of the 106 ms decode step.
Test: per-kernel trace of one decode step at batch 16.
Predict: MoE kernels sum to 70-90 ms.
Cost: one boot, 10 minutes.
```

A hypothesis without a prediction cannot fail and therefore cannot teach. A refuted hypothesis is a result: record it and move on. Prefer hypotheses that can be tested at a cheap level and that, if true, account for most of the gap. Check the knowledge tree before spending cluster time; a prior campaign may have tested it.

## 5. Test at the cheapest level that discriminates

| Level | Iteration time | Answers |
|:--|:--|:--|
| Analytical model | minutes | Whether an idea can win on paper |
| Kernel microbenchmark, one device | seconds | Kernel speed at the real shapes |
| Single server boot, held, variants run against it | minutes per variant | Scheduler and cache behavior under the real workload |
| Full evaluation round | tens of minutes to an hour | Acceptance only |

Move a question down to the cheapest level where the hypotheses under test give different answers. Climb only when the cheaper level cannot separate them. Kernel tuning belongs on one device with synthetic inputs at the production shapes. Scheduler and cache questions need the real request stream but not a reboot per variant: boot once, hold the server, run variants against it.

Run independent hypotheses in parallel across nodes. Never test two changes in one measurement.

## 6. Change one variable, compare paired

- Same node, same allocation, same seed, baseline then candidate. If the change needs a reboot, boot both from the same staged checkout.
- Use the protocol from step 1. Report the median of the repetitions and the spread, not one number.
- Accept when the improvement exceeds the detectable delta and the gate passes. Reject otherwise, including "improved but inside the noise".

## 7. Re-check correctness every time

Run the accuracy gate with every benchmark, never only at the end. Optimizations that drop context, change numerics, or skip work look like wins on the latency metric. A change that fails the gate is a bug regardless of its speedup.

Classify every candidate before testing it: does it compute the same numbers (a faster kernel on the same weights and math, differing only at rounding), or different numbers (a lower-precision activation or weight format, a re-quantized checkpoint)? A task's gate is usually a handful of probes that catch a broken kernel but not a slightly worse model, so it is sufficient only for same-numbers changes. For different-numbers changes, either exclude them by policy up front or add an agreement test against the current path on real inputs (next-token top-1 agreement and mean KL over a few thousand tokens) with thresholds set before the experiment. State the policy in the campaign notes before the first candidate so it is not decided after seeing a speedup.

## 8. Keep the ledger

The ledger lives in the task repository next to the harness, at `.vibesys/tasks/<task>/LEDGER.md`, as a markdown table with one row per experiment and this fixed column set, because the curation skill consumes it mechanically:

```
| date | node | tree | image | hypothesis | prediction | measured | gate | decision | scope |
```

- `tree`: the checkout SHA under test. `image`: the container or environment version.
- `measured`: the median and spread, not one number. `gate`: pass or fail with the count.
- `decision`: accept, reject, or refuted, with one clause of reason.
- `scope`: the author's guess at where the finding holds (backend, gfx target, SKU, memory model, software version, or site). Site-scoped rows are never promoted.

Record dead ends with the same care as wins; the ledger's value is that nobody retries a refuted idea. Every experiment gets a row, including the ones that only produced a blocker and its fix.

## 9. Re-profile after each accepted change

Fixing the top term changes the shape of the profile. The next top term may be a stage that was invisible before (collectives behind a slow kernel, scheduler gaps behind long steps). Return to step 3 with a fresh profile; do not plan several steps ahead from a stale one.

## Stop criteria

Stop the campaign, or change the design, when any holds:

- Measured is within the refined bound's margin (under about 1.3x) for the current design.
- The remaining gap is spread over many small terms, each below the detectable delta.
- The next hypothesis cannot win on paper (the step 2 model says its best case is below the delta).

At a stop, or at any phase end, run `curate-serving-knowledge` to promote the ledger's verified findings into the tree.

## Pitfalls

- **Optimizing before the noise floor is known.** The most common failure. A 15 percent "win" measured once on a fast node is noise.
- **Testing at the wrong level.** Tuning a kernel through full server rounds costs an hour per data point where a microbenchmark costs seconds.
- **Two changes per experiment.** Attribution becomes impossible; the second change is often the one that mattered.
- **Trusting a stale profile.** A profile taken before the last accepted change no longer describes the system.
- **Benchmark overfitting.** A change that helps one seed and one concurrency but not another is not an improvement. Check a second seed and a second concurrency before accepting.
- **Skipping the bound.** Without it there is no stop criterion, and tuning continues past the point of diminishing returns.
- **Correctness at the end only.** A gate failure discovered after five accepted changes means re-bisecting all five.
- **Skipping the tree.** Rediscovering a documented blocker costs a day; grepping for it costs a minute.
