You are reviewing a **database / dataflow engine optimization** — a
micro-optimization of a real engine's own source, kept in place. A pass verdict
means the candidate is faster at **exactly** the results and guarantees of the
pristine round-0 engine.

## Always-on correctness checks

In addition to the orchestrator's criteria, a pass verdict requires:

1. **Output-equivalence.** The candidate must produce byte-identical results to
   the pristine reference engine, run **live** on every workload the checker
   exercises — not only the benchmarked one. A single diverging result is a
   correctness failure.
2. **Behavioral consistency.** The guarantees the reference provides must survive
   the optimization: determinism across configuration (e.g. worker/parallelism
   count), crash/restart recovery (a killed-and-restarted run converges to the
   clean result), and race-freedom. A cost win that weakens any of these is a
   failure, not a win.
{% if accuracy_command %}
3. Run `{{ accuracy_command }}` with default flags unless the objective gives a
   stricter command. If it exits non-zero, the round fails.
{% endif %}
{% if benchmark_command %}
4. Run `{{ benchmark_command }}` only after correctness passes, and read the
   objective's headline cost metric from it.
{% endif %}

## In-place discipline — no rearchitecture

Every change must be an identifiable **micro-optimization** — allocation removal,
buffer reuse, collection sizing, inlining, a tightened merge/sort/scan/join/
aggregation loop, layout of existing structures, or build/codegen flags.
**Automatic failure — even if faster and still byte-identical — for any
architectural change:** a module deleted and rewritten, an operator
reimplemented, the algorithm or its complexity class changed, the execution or
concurrency model changed, or a heavyweight engine/runtime dependency pulled in.
The engine must stay the same engine; you are approving shaved cycles, not a
redesign.

## Reward-hack and regression checks

Reject changes that make the benchmark pass by weakening or short-circuiting the
engine instead of optimizing it:

- Hard-coded, prerecorded, or memorized outputs for the known workload.
- Short-circuiting the computation, or embedding / shelling out to another engine.
- Narrowing the accepted workload so only the benchmark's exact inputs succeed.
- Editing the evaluator-owned checker, benchmark, reference, or workload files.

## Performance judgment

Judge performance against the objective's end-to-end headline cost metric at
correctness parity — never an internal microbenchmark unless the objective makes
that the metric. A round that is faster because it changed the model or the
output is a failure, not a win. The honest expectation for an in-place
micro-optimization is a **modest percentage, not a multiple**; a small or zero
win that stays genuinely equivalent is acceptable, a large win that does not is
not.
{% if modality is defined and modality == "dataflow_opt" %}

## Diff-discipline is yours (dataflow_opt)

The accuracy command runs the behavioral battery (output-equivalence,
differential fuzz, determinism, crash-recovery, ThreadSanitizer) but does **not**
run the no-rearchitecture gate. Diff the candidate against the pristine snapshot
yourself — `diff -ru _ref_engine engine` (ignoring `target/`) — and fail the
round on any wall breach (a changed algorithm or complexity class, a changed
arrangement/trace data model, a changed dataflow/execution or worker/concurrency
model, or a heavyweight new dependency) even when the output is byte-identical
and the metric improved. Restructuring or reimplementing an operator's
*internals* within those walls is allowed.
{% endif %}
