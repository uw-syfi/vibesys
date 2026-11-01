---
name: microservice-optimization
description: >-
  Restructuring the request graph of a microservice application to cut
  end-to-end latency. Activate on serialized cross-service calls,
  critical-path contribution, sequential dependencies that could run in
  parallel, work that could move off the request path, batching repeated calls
  to one service, or collapsing a call edge by co-locating or merging two
  services.
---

# microservice-optimization

Two strategies for cutting end-to-end latency by changing the shape of the
request graph, not by tuning parameters inside a fixed topology.

| Strategy | Changes | Reach for it when |
| --- | --- | --- |
| 1. When work happens | Timing of calls relative to the request | The critical path is a chain of calls that need not be a chain |
| 2. How many calls happen | Count of cross-service calls | The critical path is dominated by call count or per-call overhead |

Tuning within the current topology (connection pools, gateway upstreams, cache
usage, database indexes, worker counts, serialization) is covered by the
orchestrator's standing task guidance and is not repeated here. Reach for this
skill when the evidence points at the graph rather than at one service's
configuration.

## Establish the evidence first

The two roles see different evidence. Neither should act on the other's.

**Orchestrator.** You see the profiler's prose summary, not the trace graph.
Select a strategy only when that summary names a specific bottleneck: a
service, an operation, or an edge with critical-path contribution. A ranking of
services by aggregate latency is not sufficient. Aggregate cost and
critical-path contribution are different quantities, and the expensive service
is frequently not the one holding the request open. When the summary names no
edge, plan a profiling round rather than guessing one.

**Implementer.** You can read the graph. Confirm the edge or the structure
before editing:

1. `trace_graphs()` to locate schema-v2 graphs.
2. `critical_path(path=..., telemetry_path=...)` for wall-clock attribution.
3. Read `nodes_by_contribution` for ranked contributors, and the representative
   segments for the order in which calls actually occur.

Overlapping sibling calls are not additive. Two calls each showing 10ms of
inclusive latency may cost 10ms together, not 20ms, in which case
parallelizing them gains nothing. Representative segments distinguish
sequential work from overlapping work; flat span aggregates do not.

Critical-path scope is `synchronous_request`. Async and linked relationships
are excluded and counted in `async_relationships_excluded`. Work moved off the
synchronous path leaves the critical path by construction, so a before/after
critical-path comparison cannot by itself show that the work got cheaper.
Confirm every claim against the benchmark's `primary_value`.

## Strategy 1: change when work happens

Two forms, in increasing risk.

**Parallelize independent sequential calls.** Two calls issued one after the
other, where the second does not consume the first's result, can be issued
concurrently. Verify the independence in the code, not from the trace: the
trace shows they are sequential, not that they must be.

Preconditions: no data dependency, no ordering requirement the correctness
contract relies on, and a downstream that tolerates the added concurrency.
Check the second condition against the accuracy oracle's properties, not
against the benchmark.

**Move work off the request path.** Precompute it, do it at write time, or run
it in the background. This removes the work from the measured path rather than
making it faster.

This form changes consistency semantics, and that is where it fails. The
accuracy oracle independently checks read-your-write behavior, index
invalidation, deletion, and isolation. Work deferred out of a write path can
improve the benchmark and fail accuracy, which is a rejected round, not a
tradeoff. State which property you are relying on staying true before making
the change.

## Strategy 2: change how many cross-service calls happen

A ladder, cheapest and most reversible first. Take one step per round and
measure. Each step subsumes the one before it, so a step that does not pay off
is evidence against the steps above it.

| Step | Change | Keeps |
| --- | --- | --- |
| 1. Batch | Coalesce repeated calls to one service into one call | Both services, the network hop, the process boundary |
| 2. Co-locate | Schedule both services on one host so the call stops crossing the network | Both services, the RPC, the process boundary |
| 3. Merge processes | The call becomes an in-process function call | Both codebases, separate storage |
| 4. Merge storage | The two services share one datastore | Nothing of the boundary |

Step 1 changes the callee's internal API and is usually the largest win per
unit of risk when a call is issued in a loop or once per result row. Look for
N+1 patterns in the representative segments before reaching for step 3.

Step 2 is worth measuring on its own because it separates two causes that get
conflated: network transit and serialization cost versus the process boundary
itself. If co-location captures most of the gain, steps 3 and 4 are buying
little.

Steps 3 and 4 are hard to reverse. Step 4 in particular is what usually makes
step 3 pay off, and also what makes the change difficult to unwind if a later
round wants the boundary back.

## What is fixed and what is not

The externally visible API behavior and the correctness contract are fixed.
Everything else is in scope: internal architecture, programming languages,
service decomposition, storage systems, caches, RPC mechanisms, deployment
topology, and internal APIs.

This means step 3 and step 4 are legitimate optimizations, not contract
violations. It also means the accuracy oracle, which exercises the external
contract with randomized cases, is the check that matters. A restructuring that
passes the benchmark and fails accuracy has not found anything.

## Verification

For any change from either strategy:

- The benchmark's `primary_value` is the result. Critical-path contribution,
  span latency, and trace graphs are diagnostic evidence for choosing the
  change, never evidence that it worked.
- Re-read the critical path after the change. A restructuring that moves the
  bottleneck to a different edge is a partial result worth recording, and it
  names the next round's target.
- Compare graphs only across matching workload identity and window count. The
  `critical_path` tool rejects mismatched pairs; do not work around it.
- When a step in the strategy 2 ladder produces no measurable gain, record that
  and stop climbing. The remaining steps cost more and are less reversible.
