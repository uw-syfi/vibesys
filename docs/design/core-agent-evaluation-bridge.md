# Agent evaluation tool on the core path (SW-3b, D396)

Agents keep a narrow in-turn tool to submit a measurement for their own principal
and snapshot. On the core path the tool call is an input to vs-core. Core is the
only authority on admission, budget and identity. The tool server holds no policy.

## Findings that shape the design

1. The runtime shell (`CoreRuntime`) is serial. `dispatch_one` awaits the executor for
   the whole agent turn, so a tool call that arrives mid-turn can be queued with
   `submit` but cannot be stepped or answered until the turn ends.
2. A suspended turn resumes through a `Continuation`, published only after the
   work-in-progress checkpoint, and `_validate_new` requires every `jobs` entry to be
   an already-owned job in the same scope. The measurement must therefore be owned
   (admitted and submitted) before the turn's suspension is committed.
3. Nothing in vs-runtime produces `TurnObserved.suspension` yet, and the host loop
   (`drive_core_run`) is still unavailable on integ/compose until COMPOSE-A lands it.
4. `tests/e2e/test_dynamic_loop_smoke_e2e.py::test_core_path_runs_the_fake_slurm_search_with_zero_legacy_execution`
   does not exist yet; it is created here.

## Event and requests

- New core event `AgentMeasurementRequested(scope, principal, snapshot, plan, call_id)`
  in the Evaluation area (routed through `EVENT_TO_SUBAREA`). `call_id` is minted by the
  tool server per call and is the idempotency key.
- Core step on it: admit (the principal holds an `Evaluate` grant for the scope and the
  snapshot is the principal's current one), compute `MeasurementIdentity` from the plan,
  charge the `SubmissionBudget` for that identity against
  `Limits.max_measurement_submissions`, and emit one `SubmitMeasurement`. A duplicate
  `call_id` or a duplicate identity beyond budget emits no request and yields a typed
  rejection.
- The measurement request id is deterministic from `(scope, identity, ordinal)`, so the
  resource handle the executor derives (`handle_for(request_id)`) is known to core
  before the executor runs. That lets the continuation name its job without a round trip.
- Rejections are `AgentMeasurementRejected(call_id, reason)` with a closed enum
  (`NOT_GRANTED`, `STALE_SNAPSHOT`, `BUDGET_EXHAUSTED`, `DUPLICATE`, `CANCELLED`,
  `DEADLINE`). Nothing is silently dropped.

## Reply: one continuation, never a poll

The tool call returns immediately with the handle (or the typed rejection). The agent
then ends its turn with the existing "waiting for evaluation" reply. The runtime yield
producer turns that reply into `TurnObserved.suspension = Continuation(jobs=[handle])`.
Core publishes `TurnSuspended` after the checkpoint and later emits `ResumeAuthorized`
exactly once, when the owned job reaches a terminal `MeasurementResult` or the deadline
fires. There is no status or wait tool on the core path: the profiler-polling waste in
r23late (585 s) cannot recur because no tool can poll.

Ordering: the shell commits the admission event first (at the next step boundary, via
the durable `pending_inputs` outbox), so the job is owned before the turn's suspension
is committed. A turn that ends without yielding leaves the measurement owned; its result
reaches the strategy as an ordinary `MeasurementResult`.

## Cancel, deadline, rejection

- Cancel: run cancel clears pending tool events; an admitted job is cancelled through
  the existing `CancelOwnedJob` intent. A late tool call after cancel gets `CANCELLED`.
- Deadline: `DeadlineReached` resumes the continuation once with a deadline outcome; a
  tool call after the deadline gets `DEADLINE`. The job is cancelled by the same intent.
- Rejection: returned to the agent in the tool reply; no budget is charged.

## Mechanism reuse

| Piece | Reuses |
|---|---|
| Identity, budget, receipts | `_measurements._identity`, `SubmissionBudget`, `PreparedSubmissionReceipt` |
| Submission | `SubmitMeasurement` and the vs-runtime evaluation executor |
| Resume | `Continuation`, `TurnSuspended`, `ResumeAuthorized`, `DeadlineReached` |
| Durable intake | `RuntimeRecord.pending_inputs` outbox |
| Agent wire | `vs_evaluation.agent_mcp` MCP client, socket JSON models, role grants |
| Snapshot | `SemanticEvaluationBackend` snapshot and `semantic_evaluation_identity` |
| Host wiring | `CoreServices`/`CorePolicy`; `_refuse_unbridged_tools` stops refusing this tool |

New: the core event and its step, a runtime intake that turns tool calls into events and
wakes the loop, the yield producer, and a core-backed socket service speaking the existing
wire contract (minus the status, wait and cancel tools).

## vs-core interface change and consumers

Adds one event and one rejection event to the `CoreEvent` and `StrategyEvent` unions.
Consumers to update in the same PR: `api/routing.py` and `api/evaluation.py` routing
tables, `RunView` projection, the dynamic strategy `on_event`, and exhaustive matches in
`vs-runtime` event journaling. No existing event changes shape.

## Legacy modules made dead (for SW-9)

- `vs_evaluation/agent_service.py` (`EvaluationAgentService`), once the core-backed service replaces it
- `vs_evaluation/agent_mcp.py` tools `evaluation_status`, `validate_evaluation_wait`,
  `evaluation_availability`, `cancel_evaluation` (core path has no poll or wait tool)
- `src/vibesys/run/evaluation_backend.py` `SemanticEvaluationBackend` admission and budget
  (snapshot creation survives as an input to the event)
- `_evaluation_tool` in `src/launch/composition.py` legacy branch
- Legacy loop wait handling for `WaitingForEvaluation`
