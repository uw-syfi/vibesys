# Agent evaluation tool on the core path (SW-3b, D396)

Agents keep a narrow in-turn tool to submit a measurement of their own workspace.
On the core path a tool call is an input to vs-core. Core is the only authority on
admission, budget and identity. The tool server holds no policy.

## Findings that shaped the design

1. The runtime shell (`CoreRuntime`) is serial: `dispatch_one` awaits the executor for
   a whole agent turn, so a tool call that arrives mid-turn cannot be stepped by the
   loop. The shell gains `admit(event, now_at)`: it runs core's pure step against the
   committed state plus the admitted inputs still queued, queues the event for the next
   commit, and returns the transition. The tool answers from that transition.
2. A suspended turn resumes through a `Continuation`, published only after the
   work-in-progress checkpoint, and `_validate_new` requires every `jobs` entry to be an
   already owned job in the same scope. The submission is therefore admitted before the
   turn's suspension is committed (the queued event commits first).
3. The state store's time watermark never moves back. A tool call is stamped before the
   turn's own commit, so the shell now stamps every commit, lease check and publication
   no earlier than the last commit.

## Event and requests

- Core event `AgentMeasurementRequested(scope, plan, call_id)` in the Evaluation area.
  `call_id` is minted per call and is the idempotency key. The principal is the scope:
  the tool token binds one core scope (run, or attempt generation).
- Core step: reject when the run is not RUNNING (`RUN_STOPPING`), the scope is not a
  current admitted owner (`NOT_ADMITTED`), the plan has no candidate (`INVALID_PLAN`), or
  the identity's budget is spent or the plan exceeds a run limit or deadline
  (`NOT_ALLOWED`). Otherwise charge the shared `SubmissionBudget` for the identity and
  emit one `SubmitMeasurement` with no decision id. Every call is recorded in
  `EvaluationState.agent_calls` with its `AgentRejection` or its request id. A replayed
  `call_id` changes nothing.
- A call for a candidate its scope already evaluates is a repeat, not a second charge (below).
  An agent's call, admitted or refused, and the end of its job are answered to the agent
  only: core does not report them to the strategy as a `MeasurementResult`. A strategy takes
  a result of its scope for the Measure it awaits, so reporting an agent's job there handed
  a waiting Measure another submission's local-validation evidence (the v13 round that
  passed review and recorded no metric).
- A decision-less submission is proven by its admitted call (`_measure_origin`), not by an
  accepted `Measure` receipt. Agent calls and `Measure` decisions share one budget.
- The measurement request id is deterministic, so the handle the executor derives is
  known before it runs.

## One evaluation per candidate and scope (repeats)

An exact candidate (same content and measurement policy, so the same `MeasurementIdentity`)
is evaluated once per core scope. The scope's `SubmissionBudget` receipts are the one record
of "this candidate's evaluation"; the bridge keeps no second one. When a call finds a
budget for its identity that would not admit another submission (the earlier submission is
running, or ended and cannot be redone), core records the call as a repeat
(`AgentCall.repeat`) naming the earlier submission and its deadline, charges nothing, and
the reply carries the same handle (`repeat: true`), plus `outcome`, the evidence ids and
`stages` (each stage's verdict and measured values with unit and direction, read from the
evidence ledger) when it has ended. A released, retryable infrastructure failure is not a repeat: the next
call is a real retry and is charged. A call for another candidate is another charge.

Who may read whose evaluation: every role that holds the scope. A workstream's roles
(implementer, judge, profiler) run in its attempt's scope, so a judge reviewing a candidate
gets the evaluation its implementer made, and the other way round. Another attempt's
evaluation is not readable, even for identical content: its evidence and its budget belong
to that attempt, and sharing them would let siblings of a population read each other's
measurements. A reply is a handle, evidence ids and the measurements those records hold, never another
scope's data. The resumed turn's prompt and the judge's prompt state the same measurements,
because the measured number is not withheld from any role that holds the scope.

Owed submissions (#1868, #2021) are only the promise that a reply the agent never read is
returned on the retry; they hold the call and core says what it decided. They do not decide
what a repeat is, so a repeat that was lost is owed like any other reply.

## Reply: one continuation, never a poll

The tool replies at once with the handle or a typed refusal naming the reason. The agent
validates its wait (`validate_evaluation_wait`, own handles only) and ends the turn with
the existing `waiting_for_evaluation` reply. The runtime yield producer
(`AgentEvaluationBridge.yielded`, passed to the session executor as `TurnYields`) turns
that into `TurnObserved.suspension`. Core publishes `TurnSuspended` after the checkpoint
and later `ResumeAuthorized` exactly once, when the owned job is terminal and the ledger
has closed its submission, or at the deadline.

When a job ends, the ledger first takes its end as its submission's own, then the
evaluation history is derived and the continuation woken. A waiting continuation
publishes the history itself when it authorizes, so the job's end emits it only when no
continuation waits (one signal per round, or core reports a signal cycle).

There is no status, wait or cancel tool, so the profiler polling of r23late (585 s)
cannot recur.

## Cancel, deadline, rejection

- Cancel and stop: the run leaves RUNNING, new calls get `RUN_STOPPING` (the tool tells
  the agent the run is stopping), and admitted jobs drain through the existing cancel
  records.
- Deadline: once the wait's deadline passes, core resumes the continuation once with a
  timeout outcome; later calls get `RUN_STOPPING` or `NOT_ALLOWED` by plan deadline. The
  limit counts from the job's start: while a job waits in a queue the wait has no deadline
  (only the run's own deadline and stop end it), and each queued interval moves the
  submission's deadline later by its length (`vs_core._wait_clock`).
- Rejection: returned in the tool reply, nothing charged.
- A resumed invocation reports released and children complete, like any dispatched turn,
  so its attempt drains. A yielded invocation holds nothing once its continuation was
  resumed or cancelled, so the run can close.

## Mechanism reuse

| Piece | Reuses |
|---|---|
| Identity, budget, receipts | `_measurements._identity`, `SubmissionBudget` |
| Submission | `SubmitMeasurement` and the vs-runtime evaluation executor |
| Resume | `Continuation`, `TurnSuspended`, `ResumeAuthorized`, `DeadlineReached` |
| Agent wire | `vs_evaluation` socket models; new `agent_wire.py`, `agent_core_mcp.py` |
| Snapshot | workspace `snapshot_and_retain`, reused while content is unchanged |
| Host wiring | `CorePlan.agent_evaluation`, `CoreServices.agent_evaluation`, `ResolverInputs.tool_servers`, `SessionServices.yields` |

## vs-core interface change and consumers

Adds `AgentMeasurementRequested`, `AgentCall`, `AgentRejection` and
`EvaluationState.agent_calls`. Consumers updated: `_routing`, evaluation area, runtime
event journaling. No existing event changes shape. `MeasurementResult` of an agent job
reaches the strategy as an ordinary result; the dynamic strategy ignores results that
are not for its awaiting measurement step.

## Legacy modules made dead (for SW-9)

- `vs_evaluation/agent_service.py` (`EvaluationAgentService`)
- `vs_evaluation/agent_mcp.py` tools `evaluation_status`, `validate_evaluation_wait`,
  `evaluation_availability`, `cancel_evaluation`
- `src/vibesys/run/evaluation_backend.py` `SemanticEvaluationBackend` admission and
  budget (the identity and snapshot helpers survive)
- `_evaluation_tool` legacy branch in `src/launch/composition.py`
- Legacy loop handling of `WaitingForEvaluation`
- The Slurm shell gate path, which only the loops not yet on the core path use (the
  dynamic loop's agents have the evaluation tool and are given none of it): the broker's
  `gate` operation, `Gates`, `GateRunner` and `GateKind` in
  `libs/vs-sandbox/src/vs_sandbox/host_command_broker.py`, `gate_runners.py`,
  `slurm_command.py` and `benchmark_output.py`, the `--gate` mode of
  `host_command_client.py` (the `vibesys-gate` launcher), `bridged_agent_paths` and
  `planned_gates` in `vs_runtime/_host_command_bridge.py`, the gate branch of
  `SlurmEnvironment._open`, `src/vibesys/prompts/environments/_gate_client.j2`, and
  `RunEnvironmentRequest.evaluation_tool` with `offers_evaluation_tool`, which exist only to
  tell the two apart. The older loops (agent single and multi, profile-guided, plain/issue
  queue, evolve) run their mid-turn gate and framework post-round trusted evaluation through
  it; delete it per loop as each is ported (roadmap #1952, #1649), and finally in phase 6.
