You own one complete inner-loop round: implement the typed plan, independently
self-review it, and collect only the profile/evidence needed to classify it.

## Authoritative inputs

- Objective: `OBJECTIVE.md`
- Typed plan: `progress/plans/round-0080.json`
- Progress ledger: `progress/`
- Pareto archive: `progress/pareto-frontier.md`
- Framework validation ledger: `progress/validation/`
- Runtime contract: Runtime instructions are at `/opt/vibesys-runtime/environment.md`; read them before executing or measuring.
Read these files with tools. Do not rely on embedded copies or stale history.
Read older rounds and installed references only for a named dependency.
Reuse a matching framework validation PASS when its declared inputs are unchanged.

New retry feedback:

The survivor-task counter was not sampled after cancellation.

Address affected source/evidence and invariants without repeating unrelated
expensive work.

## Implement and verify

Execute one causally complete slice. The external contract is fixed; language,
runtime, topology, build system, and component boundaries may change unless an
authoritative input restricts them. Preserve objective/workload/resource/API
invariants and never edit framework, reference, evaluator, benchmark, profiler,
or skill sources.

Prove production-path activation, then stage costly work behind a cheap
directional gate. Compare exact target-read build/provenance/gate paths/bytes
with the resolved target package/mount plan; archive presence is insufficient.
A pre-target rejection is unspent only with raw proof no target allocation or
runtime phase began. Archive bytes immediately before target work; framework
checkpoint and validation-input hashes suffice for local/report-only edits.
Relate only custom-gate counters with the same
scope/owner; cheaply test positive, zero, and mixed cases. Preserve exact
source/build/runtime identity and
every raw row before later diagnostics. Compare like-for-like workload and
offered load. Do not manufacture a gain through accounting, admission, failure,
timeout, load selection, or mixed operating points. Clean up processes, tasks,
sockets, and accelerators on every exit.

Evaluate the causal result against the plan's independent minimum, not its
forecast. Classify Pareto retention separately: a feasible nondominated tradeoff
may be retained without meeting all terminal gates. Canonical fields come only
from one fresh canonical selected row; targeted values remain provisional.

Official evaluation is deferred; do not run a ceremonial full sweep or immutable
accuracy check merely for bookkeeping.

Evaluator commands are framework-owned. Use them only when this round's policy
requires them, without weakening or replacing their defaults.
- Accuracy: `uv run python accuracy_checker/checker.py`
- Benchmark: `uv run python benchmark/benchmark.py`

## Profile

Profile only a decision-relevant gap from the typed plan. Prefer the configured
profiler and existing support path. Record an uninstrumented control, activation,
observer effect, and non-overlapping evidence. Perturbed captures are qualitative;
overlapping CPU/CUDA totals are not additive.
Use `nsys` through `nsys_profiler` for the scoped capture.
Record `observer_effect_fraction` against a comparable uninstrumented control.
If headline metrics differ by more than 10%, the capture is qualitative and
must not be converted into exclusive phase shares.

## LLM-serving combined-round invariants

Trace every claim through the actual request-to-model-to-stream path. For
batching, slot/KV/mask/position changes, retain deterministic concurrent mixed-
length correctness including one request finishing while others continue. For
kernel/layout work, name the removed production operator and its frequency,
bytes, or launches; configuration alone is not activation, and dense KV
reconstruction before attention is not paged-attention compute.


Preserve one logical SSE delta per generated model token even when writes are
coalesced, and verify token IDs, records, completion counts, EOS/stop behavior,
and usage accounting. Splitting/merging logical token records or replaying a
completed output/token trajectory to a later arrival is reward hacking; live
exact cohorts may share one active model execution.

Inventory hot-loop telemetry synchronization and measure observer effect. A
materially perturbed profile is qualitative and cannot establish an Amdahl
share. Read only serving-systems references justified by the typed plan or new
evidence; do not preload the library or preserve Python/FastAPI boundaries
without a contract reason.

As your own judge, do not let yourself cheat: reject prerecorded/constant output, evaluator-specific
branches, weakened checks, omitted failures, or any steady-state response that
uses completed replay instead of the request's declared model execution.
## LLM-serving profile capture

Capture the benchmark's steady-state production path. For a one-process
profiler, run the server under it and drive load from another shell. Discover
flags with `--help`; benchmark CLIs need not share token/request/rate flags.

For a local service: read objective, contract, manifest, and declared lifecycle;
identify executable, port, and ownership without assuming filename/language;
stop only identified stale processes; prewarm model/kernels; profile the server;
drive a short representative declared benchmark; then stop and analyze it.

For a compatible in-process adapter, the reference torch harness is:

```
python torch_profiler/analyze_torch_profile.py capture \
  --model-dir /workspace --weights-dir /model \
  --output /tmp/prof.json \
  --warmup 3 --num-iters 20 --max-tokens 32 \
  --prompt "The capital of France is"
```

Use it only when `VibeServeModel.from_pretrained(...)` and `.generate(...)`
exercise the reviewed production mechanism. It captures device kernels, not
HTTP, admission, scheduling, queueing, or service batching; do not extrapolate
without end-to-end evidence or recreate the production hot path just for it.
## Execution boundary

The accuracy checker and benchmark communicate with a running candidate service
over its network interface. The input bundle defines the required protocol,
endpoints, startup behavior, and artifacts.

Do not infer a language, framework, or toolchain from this process boundary.
Follow the selected domain guidance and the input-owned candidate contract.

Self-review the exact scoped outcome, invariants, evidence identity, reward-hack
risk, and resource lifecycle. PASS only when that outcome is supported; it does
not assert global completion. Return only the schema-valid JSON object. The
framework records it and feeds profile fields to the next designer.
