# Serving Performance Modeling

Build a calibrated analytical model before choosing expensive serving-system
optimizations, and refresh it when progress plateaus.

## Prerequisites

- A faithful end-to-end benchmark result for the target workload.
- The model architecture, precision, hardware, and operator constraints.
- At least coarse timing or profiler evidence from the production serving path.
- Separate TTFT, TPOT, latency, throughput, failure, and offered-load data when
  the objective exposes them.

Do not treat an uncalibrated hardware peak or a profiler self-time summary as a
prediction of service performance.

Calibrate profiler observer effects before using phase attribution. Compare the
profiled run with an uninstrumented control at the same candidate, workload
shape, and operating point. If the headline metric differs by more than 10%,
or the capture changes synchronization, scheduling, or the critical path, mark
the capture `perturbed`. If no comparable control exists, mark it
`uncalibrated`. A perturbed or uncalibrated trace can prove path activation,
ordering, graph coverage, fallback, or the presence of work, but its accumulated
durations must not become exclusive phase shares, removable milliseconds,
Amdahl ceilings, or hypothesis rankings. Obtain a lower-overhead measurement or
use a causal A/B experiment instead of informally discounting observer overhead.

## Model three different ceilings

| Ceiling | Question | Inputs |
|:--|:--|:--|
| Hardware/workload | What could this model and workload approach on this hardware under ideal execution? | FLOPs, bytes, usable compute and bandwidth, request shape |
| Current architecture | What could the present scheduler, batching, execution, and transport design approach after plausible optimization? | Measured phase times, occupancy, overlap, batching, residual time |
| Hypothesis | What is the maximum end-to-end gain from the proposed mechanism? | Cost-center share and the fraction the mechanism can remove |

Keep these ceilings separate. A mechanism can approach its own ceiling while
leaving the architecture far below the objective.

## Start from the target gap

For a throughput objective:

```text
required_speedup = target_throughput / current_throughput
```

For a latency objective, use the corresponding reduction factor and preserve
the other objective constraints. Compute the gap at the same workload shape and
operating point; do not mix concurrency, sequence lengths, precision, request
mode, or failure policy.

## Account for end-to-end time

Use non-overlapping phases where possible:

```text
T_client = T_queue + T_prefill + T_decode + T_host + T_transport + T_residual
```

If phases overlap, model the critical path rather than summing their durations.
Always retain:

```text
coverage = explained_nonoverlapping_time / client_observed_time
residual = client_observed_time - explained_nonoverlapping_time
```

A large residual is a profiling result: locate it before optimizing a fully
accounted minor phase. Correlate client, HTTP, scheduler, model-runner, and GPU
timestamps when the service boundary matters.

For an asynchronous GPU runtime, a CUDA event pair around a host-side scope
measures device work queued between the events, not exclusive time owned by the
Python scope. Do not add that duration to the scope's CPU time or interpret it
as independent work without a timeline or synchronization boundary that proves
the attribution.

Treat a coarse profiler scope as an envelope, not attribution to whichever
operation its label suggests. Before choosing a mechanism from a hot composite
scope:

1. Inspect every operation enclosed by the scope.
2. Count each operation at its real frequency per step, request, and useful
   token.
3. Separate device synchronization and transfers from Python bookkeeping,
   cache metadata, tokenization, serialization, locks, and queue handoff.
4. Use nested low-overhead counters, a timeline, or a one-variable causal A/B
   probe when the ranking depends on one constituent.

For example, a scope named `sampling_postprocess` may include batched argmax,
per-request `.item()` synchronization, KV metadata updates, detokenization,
stop-string scans, and stream enqueue work. A large envelope proves only that
some enclosed work or dependency is expensive; it does not justify assigning
the measured share to detokenization or any other constituent. Prefer the
highest-frequency synchronization or transfer as the first discriminating
probe when source inspection exposes one.

Before proposing generic CPU/GPU overlap, inventory every host synchronization
on the token-step path and multiply it by execution frequency. Search for
`.item()`, `.tolist()`, CPU transfers, tensor truth tests, explicit
synchronization, and device-derived Python shape values. Distinguish one
unavoidable sample handoff per token step from an accidental synchronization
inside every decoder layer or request. Removing 32 per-layer barriers is a
different hypothesis and ceiling from hiding one scheduler handoff. Also draw
the data-dependency edge: work that produces the next token cannot overlap the
next forward unless that token remains on device or a cohort executes
independently.

## Bound a hypothesis with Amdahl's law

If a fraction `f` of end-to-end time is accelerated by `s`:

```text
total_speedup = 1 / ((1 - f) + f / s)
perfect_removal_ceiling = 1 / (1 - f)
```

Use a range for `f` and `s`. Reject false precision. A small bound can still
justify a cheap experiment or prerequisite, but it is not a structural path to
a much larger target gap.

## Build the device roofline

For one execution step:

```text
arithmetic_intensity = useful_FLOPs / bytes_moved_from_HBM
ridge_point = usable_compute_FLOPs_per_s / usable_HBM_bytes_per_s
roofline_FLOPs_per_s = min(
    usable_compute_FLOPs_per_s,
    arithmetic_intensity * usable_HBM_bytes_per_s,
)

T_device_lower_bound = max(
    step_FLOPs / usable_compute_FLOPs_per_s,
    step_bytes / usable_HBM_bytes_per_s,
)
device_token_ceiling = useful_tokens_per_step / T_device_lower_bound
```

Use measured or defensibly discounted compute and bandwidth, not marketing
peaks. Include weight reads, activation traffic, KV-cache reads/writes,
attention metadata, padding, and communication that the execution actually
requires. Recompute for each relevant batch and context-length regime.

For a transformer decode roofline, start with a layer-by-layer parameter and
operation inventory. Reconcile the configured hidden, intermediate, head,
KV-head, vocabulary, and layer dimensions with the actual tied or untied
weights. Convert every weight touched by a decode step to bytes at the declared
precision, and include the dense Q/K/V/O, gate/up/down, and output-projection
FLOPs as well as attention FLOPs. Unless a measured cache analysis proves
otherwise, count model weights at least once per batched decode step; batch
reuse amortizes those bytes over useful tokens but does not remove the read.
An attention-only calculation is a kernel roofline and must not be presented as
the full model-serving hardware ceiling.

Dense autoregressive decode often raises weight reuse with batch size while KV
traffic grows with live context. Prefill and decode therefore need separate
models. Mixed prefill/decode scheduling needs a weighted or trace-derived model,
not a single generic tokens-per-step estimate.

## Connect device and service ceilings

For a decode step producing `B_useful` request tokens:

```text
T_cycle = critical_path(
    T_device,
    T_scheduler,
    T_sync,
    T_postprocess,
    T_transport,
)
throughput_ceiling = B_useful / T_cycle
```

Account for padding, inactive slots, graph buckets, admission limits, prefill
interruptions, failures, and queue residence. A kernel roofline does not include
these service losses.

### Audit step capacity against the target

Convert the terminal throughput target into two reciprocal feasibility checks:

```text
required_cycle_at_current_capacity = B_useful_current / target_throughput
required_useful_tokens_at_floor = ceil(target_throughput * T_cycle_floor)
```

Use seconds consistently. Compare `required_cycle_at_current_capacity` with the
whole-model device lower bound plus a defensible service-overhead margin. Compare
`required_useful_tokens_at_floor` with the observed active batch, admission and
graph-bucket limits, and the memory-feasible capacity after accounting for
weights, KV cache, activations, graph-private buffers, and allocator reserve.

If the required cycle is below the device lower bound, the current useful-token
capacity cannot reach the target through faster execution of the same step. If
it leaves almost no margin for scheduler, synchronization, sampling, and
transport work, classify the path as high risk rather than terminally
sufficient. Rank a bounded capacity or multi-token capability experiment
against kernel work when it materially relaxes the required cycle. Do not count
queued requests as useful batch tokens, and do not assume a larger capacity is
free: validate memory, step-time scaling, graph coverage, admission behavior,
TTFT, TPOT, and end-to-end latency at the proposed capacity.

Model latency separately:

```text
TTFT = queue + tokenize + prefill + first_decode + first_transport
TPOT = steady_decode_cycle / useful_decode_progress
E2E = TTFT + remaining_decode_and_drain
```

Throughput parity that violates TTFT, TPOT, latency, accuracy, precision, or
failure constraints is not parity.

## Calibrate with measurements

1. Predict at least one measured operating point before using the model for
   planning.
2. Compare predicted and observed step time, throughput, TTFT, and TPOT.
3. Record the residual and the assumptions most capable of explaining it.
4. Prefer a small discriminating profile or A/B probe when uncertainty changes
   the ranking of hypotheses.
5. After an experiment, update both the phase estimate and the end-to-end model.

Use multiple concurrency points when saturation behavior matters. A model that
fits only the selected best row cannot distinguish useful batching from overload
or queueing.

## Separate forecasts from acceptance decisions

Use a predicted effect range to prioritize experiments and calibrate the model.
Do not use its midpoint or optimistic bound as the implementation pass gate.
Before the experiment, state a separate minimum acceptance criterion derived
from:

- benchmark noise and the confidence required to distinguish a real effect;
- binding correctness, latency, throughput, failure, and resource constraints;
- implementation complexity and ongoing maintenance/runtime cost;
- whether the change composes with or blocks the remaining route to the target.

If the forecast is 1.5x and the observed end-to-end result is 1.3x, retain the
change when it clears that independent minimum and remains Pareto-useful. Record
the prediction error and recalibrate the cost-center estimate. Treat forecast
error as model evidence, not as implementation failure. Reject or roll back a
positive result only when it is indistinguishable from noise, violates a binding
constraint, is dominated after its costs are included, blocks a more valuable
path, or misses the independently justified minimum.

Keep the minimum acceptance criterion separate from terminal sufficiency. A
change can be worth retaining without closing the entire target gap, and a
highly optimistic forecast does not create a stronger reason to discard useful
measured work.

## Turn prior interventions into empirical bounds

Treat an activated experiment as a measurement of its cost-center class, even
when the hypothesis is disproven. Before assigning headroom to a later mechanism:

1. List prior experiments that changed the same work or a superset of it.
2. Confirm activation, workload comparability, and which work the experiment
   actually removed, accelerated, or merely rearranged.
3. Use the observed end-to-end delta to constrain the new Amdahl range.
4. Explain any claimed headroom beyond that empirical bound with additional
   measured work that the new mechanism removes.

For example, if a clean prefill intervention activates at the representative
load but improves throughput by only 4%, a cache that skips only part of that
same prefill work does not have a credible 20% end-to-end bound by default. A
larger claim needs evidence that the earlier intervention left most prefill work
intact, or that the cache also removes another measured critical-path cost.

Do not infer a strict cost fraction from a weak intervention: batching may only
rearrange work, and a slow replacement kernel may hide the value of the work it
targets. Record these limitations. The requirement is to reconcile the new
estimate with prior causal evidence, not to overfit one noisy delta.

## Plateau workflow for the outer loop

Refresh the model after the first valid baseline, after a material architecture
change, and whenever the campaign plateaus.

1. Restore or identify the best trusted checkpoint.
2. Quantify the remaining target gap at comparable operating points.
3. Reconcile client-observed time with non-overlapping measured phases.
4. Convert comparable activated experiments into empirical bounds on their cost
   centers and reconcile new hypotheses with those bounds.
5. Recompute device, architecture, and per-hypothesis ceilings as ranges.
6. Name the assumptions and unexplained residual that dominate uncertainty.
7. Compare candidate mechanisms by plausible end-to-end headroom and experiment
   cost.
8. Propose either a structural hypothesis with sufficient headroom or the
   smallest measurement that could materially change the model.

Do not respond to a plateau with another local change in the same cost-center
class unless new evidence changes its bound.

## Recommended outer-loop record

```text
Model provenance:
- candidate commit / architecture fingerprint
- source benchmark and profiler artifacts
- measurement timestamp and workload identity

Current objective metrics:
Comparable reference metrics:
Required multiplicative improvement:

End-to-end accounting:
- cost center, measured range, overlap relationship, evidence artifact
- unexplained residual and coverage

Ceilings:
- hardware/workload range and assumptions
- current-architecture range and assumptions
- each candidate hypothesis's Amdahl bound
- empirical bounds from comparable activated experiments and their limitations

Model check:
- predicted versus measured result at representative operating points
- dominant uncertainty

Decision:
- selected hypothesis or discriminating measurement
- expected end-to-end range
- observation that would invalidate the model
```

## Freshness and calibration gate

A copied model is not a refreshed model. After a scheduler, cache layout,
kernel backend, precision, graphing strategy, or transport path changes, verify
that every architecture-dependent statement still describes the production
path. Record a candidate commit or equivalent architecture fingerprint and the
exact artifacts used for calibration.

Before using the model to rank another optimization, require all of:

1. Numerical FLOPs and byte assumptions for the relevant prefill/decode regime.
2. Usable device compute and bandwidth ranges with their source or discount.
3. A hardware/workload ceiling distinct from the current implementation knee.
4. A prediction for at least one retained operating point, its observed value,
   calibration error, and the residual the model does not explain.
5. TTFT, TPOT, end-to-end latency, failures, and accuracy treated as constraints
   rather than inferred from throughput.

Reject the model as stale when it names a removed bottleneck, contradicts
activation telemetry, cannot reproduce any retained measurement, or reports a
measured saturation point as though it were the hardware roofline.

## Pitfalls

- Mixing per-kernel, per-step, and client-observed metrics.
- Summing profiler categories that overlap or contain parent/child scopes.
- Treating CUDA-event time around an asynchronous host scope as exclusive GPU
  self-time, or adding it to the same scope's CPU time.
- Proposing overlap before counting hot-loop synchronization sites and proving
  that the supposedly overlapped work is not a dependency of the next step.
- Treating CUDA-graph attribution gaps as zero work.
- Archiving a disproven experiment without using its activated end-to-end delta
  to constrain later hypotheses in the same cost-center class.
- Using peak hardware specifications without an efficiency range.
- Ignoring host, scheduler, transport, or queueing time.
- Modeling successful tokens while silently discarding failed requests.
- Changing precision, workload, or semantics outside operator constraints.
- Optimizing a measured fraction whose perfect-removal ceiling cannot explain a
  material part of the remaining gap.

## See also

- [`../../OVERVIEW.md`](../../OVERVIEW.md) — roofline and prefill/decode foundations.
- [`profiler.md`](profiler.md) — collect system timelines and kernel roofline evidence.
- [`serving-benchmark.md`](serving-benchmark.md) — measure comparable end-to-end serving metrics.
- [`../platforms/`](../platforms/) — select the platform hardware notes for the target backend.
