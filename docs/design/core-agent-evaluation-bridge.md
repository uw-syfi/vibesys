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

## Samples and the comparison with the base (#3047)

An agent's evaluation is one kind of measurement among others: it runs the framework's gates
through the framework's executor on a committed revision, so its records are as trusted as the
strategy's own check, whoever asked for the run. The `purpose` of a run says who asked
(`local-validation` is the agent); it no longer decides what the run's records may prove.

Every conclusive run of a measured input is a `Sample` in core's `EvaluationState.samples`: its
commit, the measured-input digest, the evaluation (a digest of the evaluator, workload, host,
recipe, gates and accuracy gating), the host, whether the executor held it alone, the headline
reading and the base the candidate is compared with. A sample cannot be built without the
commit and the input. All samples of one input under one evaluation are one measurement: they are
aggregated, never picked among, and never refused for being several.

Core compares a candidate with its own base, the revision the attempt's workspace was opened on
(`WorkspacePlan.base`), whose measured input the runtime digests when it ensures that workspace
(`AttemptView.base_input`). The agent's evaluation is compared with it, and so is the strategy's
official check when the strategy asks for the comparison (`Measure.compared`, set by
`ctx.evaluation.measure_compared`), so an idea whose implementer never called the tool still has
one. A strategy that does not ask is measured as before, with no base run.
`vs_core._comparison` holds the statistic, which its docstring states. The decisions that need
state live in `vs_core._base_runs`:

- the base is measured when no fresh sample of it is held or being taken, once per
  (measured input, evaluation), so another attempt's run of the same base counts. The strategy's
  own baseline measurement of the input counts too, bound to the base's input the first time an
  agent names it. A base sample older than `TaskTerms.base_staleness` on the run clock does not
  count;
- a close call (within the verdict's critical number of noises of the bar, or with no estimate
  of the noise) earns the base more samples: up to two fresh ones under a declared
  `relative_noise`, up to three without one, and no more. Without a declared `relative_noise`
  it also earns the candidate's own input runs in its scope, up to three in all, since a
  repeated call for unchanged content is answered with the evaluation already made. Three runs
  a side that all read alike can never give a verdict, so nothing more is run for them. A base
  run that lands without growing the base's fresh samples (one went stale while it ran) is the
  last run a close call earns either side in that scope (`base_run_stalled`);
- these decisions are re-taken from state whenever any run of either side ends, with a sample
  or without, so a base run that dies (drained with its attempt, out of queue allowance, failed
  or refused) is replaced for every candidate that waited on it;
- the run is core's own (`CoreRun`), in the candidate's scope. It is not the attempt's
  evaluation history, so the agent's resume never waits for it: the turn resumes when its own
  jobs end, and the comparison it reads next grows as core's runs land. The strategy's verdict
  does wait: `ctx.evaluation.measure_compared` hands back the comparison once no run that feeds
  it is under way (`StatedComparison.settling` in the run's view). Core reports the step that
  takes a comparison from settling to settled (`ComparisonSettled`), read from that same
  `settling` and so for every way a run ends, a submission core blocks before any observation
  included (`MeasurementSubmissionEnded` closes its budget entry); the flow waits for that
  report, not on a poll, and three times the measurement's limit only bounds the wait against a
  report that never comes.

The comparison reaches the agent twice: in the tool's reply for a finished evaluation
(`SubmittedReply.comparison`, absent while the base has not ended) and in the resume prompt
(`ResumeFacts.comparisons`, from `ResumeAuthorizationReceipt.comparisons`); both give, for each
logged subject, the comparison the samples fresh at that moment make, so a base sample that went
stale is never counted as current (`latest_comparisons`). It reaches the run's
events as `measurement_compared`, emitted by the commit observer from core's append-only log.
The strategy's official measurement of an input the scope already measured is answered from the
samples held (`vs_core._measurement_reuse`), and runs afresh only when the comparison is a close
call that one more sample would narrow (not one that no run can settle). Its benchmark record enters the ledger stating the mean
of the scope's samples of the input (`vs_core._comparison.as_answer`; a failed record keeps its
own reading), and each later sample of the input in the scope restates it
(`restated_answers`) while the answer is open (`OwnedJob.answer_until`): from the official
run's end until the comparison settles, or at most `SETTLE_RUNS` times the run's limit. Core
states that moment in the view (`StatedComparison.answer_until`), `measure_compared` stops
waiting at it and hands back the records as the ledger states them then, and from then on the
record, and every later official record of the input in the scope, states that figure. The
reading a strategy decodes from a record takes its headline from
the ledger's reading, so a candidate's headline in ranking is the value the comparison states; its other
objectives are the answering run's own, and each run's own reading stays in its `Sample`.

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

A resumed turn may wait again: the resume's observation carries the suspension its nested
dispatch yielded, so an implementer that sees a measurement, changes its candidate and
evaluates again holds that feedback loop inside one paid turn. Each continuation still owes
exactly one resume, and the scope holds at most one open continuation at a time.

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
  submission's deadline later by its length (`vs_core._wait_clock`). The same rule holds
  for the submission's own record, so a strategy's `Measure` (whose limit counts from its
  dispatch) and an agent's submission never time out while their job is queued.
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
