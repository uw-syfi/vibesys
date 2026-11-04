---
name: custom-rpc-optimization
description: >-
  Design and validate custom RPC policies for microservices using measured
  request behavior and framework hooks. Use when optimizing admission,
  concurrency, scheduling, routing, retries, hedging, deadlines, cancellation,
  or cross-service feedback with rpcstack or an equivalent RPC framework.
---

# Custom RPC optimization

Choose a policy from the measured bottleneck and the task's correctness contract.
A published algorithm is a source of mechanisms, not proof that its adaptation
will improve this workload. For application request-graph restructuring, use
microservice-optimization; this skill concerns RPC behavior within that graph.

## Establish the contract and evidence

Read the task's objective, allowed edit scope, evaluator commands, resource
limits, workload and scoring rules before editing. Keep offered load, SLOs,
error accounting, correctness checks and resource allocation fixed across arms.
Goodput counts successful work within its deadline: rejected, timed-out and late
requests remain offered work and earn zero. RPC success need not mean a business
operation succeeded; describe the actual metric.

Use the available OpenTelemetry tools before selecting a policy. Discover the
capture and query its summary, operation breakdown and eligible critical paths
using the tools actually exposed in the session. For graph-capable captures,
`trace_graphs()` and `critical_path(path=..., telemetry_path=...)` locate and
attribute synchronous request paths. Verify capture revision, workload, load
point and measured window. If the role receives only a profiler report, request
these facts from the profiler rather than claiming direct trace inspection.

Separate observations from hypotheses. Inclusive child spans overlap; summing
latencies is not critical-path attribution. Sampled RPC spans do not measure
scheduler queue age, database contention, CPU utilization or network congestion
unless those sensors are explicitly present. Report missing spans, sampling and
eligibility exclusions. If telemetry is unavailable or stale, request a valid
capture or explicitly qualify the decision; never invent a measurement.

## Discover reusable mechanisms

In an rpcstack checkout, start with `examples/README.md`, `docs/PAPERS.md` and
`docs/VIBESYS.md` when present. Inventory the examples available in this revision;
do not assume a fixed count or that an example's name establishes fidelity.
Shortlist mechanisms that match the observed bottleneck, then read their source,
tests and limitations. Record inspected paths and why each shortlisted mechanism
fits or does not fit. Reading every implementation in full is unnecessary.

Map the proposed policy to its required observations, state owner, decision point,
actuator and lifetime. Check the actual framework API before implementing:

- Root/child RPC hooks propagate metadata and observe replies; service resources
  hold service-scoped controller state. Confirm generated stubs select the policy
  hooks, since default no-op hooks can make a correct module ineffective.
- Synchronous hooks cannot await credits, timers or network calls. Use an async
  call/attempt wrapper for pacing, admission waits and overall deadlines.
- A cooperative task queue changes execution between future polls. It does not
  supply CPU preemption, core isolation, NIC queues or packet-level QoS.
- Cancellation of a local future does not prove remote work stopped. Hedging and
  retries require replay-safe operations; exactly-once effects require an atomic
  storage boundary, not merely a request ID.

If a required mechanism is missing, name the gap. Extend a generic framework
mechanism only within the permitted scope; keep workload-specific policy in its
application/example module. Do not substitute an available hook and claim the
paper's stronger guarantee.

## Implement one testable hypothesis

State the measured bottleneck, proposed intervention, expected telemetry change,
expected objective change and possible regression. Prefer one interpretable
change per experiment. Paper names should not substitute for this explanation.

Scope controller state correctly: request, attempt, destination, service or
process. Keep control/feedback traffic usable during overload. Define bounded
waiting, deadline ownership, admission feedback and bootstrap behavior. Every
permit, credit and reservation must have a release path on success, error,
rejection, timeout and cancellation/drop. Do not release remote capacity until
backend completion is known; quarantine uncertain outcomes if required.

Add focused tests in the task's allowed test locations. Exercise rejection and
recovery, cancellation cleanup, state isolation, metadata propagation and a real
generated-client/server path where applicable. Use existing framework contract
tests. Do not change protected evaluator files or weaken application semantics
to pass a benchmark.

## Evaluate and leave auditable evidence

Run correctness first, then the fixed benchmark through the task's trusted
runner. Compare matching load points and inspect errors, late completions and
per-operation effects alongside the selected objective. Recheck telemetry for
the predicted mechanism; different sampling cohorts do not establish causality.
A higher overload score can coexist with lower medium-load goodput or fairness.

Retain rejected experiments. Validate a promising candidate with repeated,
interleaved baseline/candidate measurements when the run budget allows; preserve
all trials and distinguish managed selection from independent confirmation.
Do not turn a single noisy improvement into a reproducible performance claim.

In the existing hypothesis/report artifacts, record the skill used, inspected
example paths, telemetry capture identity and query results, mechanism rationale,
code revision, correctness result, measured scores and limitations. Respect the
task's writable locations. An audit should distinguish a native skill-tool call,
a successful read of this skill, and mere installation; do not self-report an
invocation that did not happen. Likewise, enabling OTel is not evidence of use:
record actual tool calls or artifact reads and how they informed the decision.
