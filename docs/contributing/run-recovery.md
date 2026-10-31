# Run recovery

This page owns what happens to a run when its process stops, by crash, by Pause or by a host restart,
and how the next process continues. It is written as the design. Parts not built yet are marked in the
[status table](#status). It changes [run-transport.md](run-transport.md) in one place: `state.pb`
becomes a cache of the log instead of the other half of a commit (see
[state.pb is a cache](#statepb-is-a-cache)).

## Why

Recovery today follows one contract: prove what happened to every open request, or block the run. A
process that dies in the middle of a request can never prove that, so any open request at the wrong
moment can wedge a run. A paused run whose planner session was starting could not be resumed:
recovery could not prove the session start, settled the check as `blocked`, and the blocked stop then
tried to publish discard requests in the transition that ended the run, which
`libs/vs-core/src/vs_core/_step.py:_registered_transition` refuses. Every later Resume failed the same
way. Two more reports are members of the same class, and neither is a separate bug: a session that ended
without acceptance (#3458) and a parked control poll that spun while a cancelled child stopped (#3451).
Both become unnecessary at the cutover (see [Open pull requests](#open-pull-requests)).

The mechanism is a value, not a file. `ObservationStatus.UNKNOWN` is a result an executor may report,
and every reducer that receives it has to invent a way to settle it. It appears 48 times in 19 `vs_core`
modules and 25 times in 17 `vs_runtime` modules. Each of those sites is a place a run can be stuck.

## Rules

Terms. A **step** is one request core asks the shell to perform: one of the 24 request kinds in
`libs/vs-core/src/vs_core/types/records.py:Request`, where `ExecuteRegisteredOperation` carries a
registered operation. Its **tag** is its `RequestId`, which is the same in every epoch and doubles as
its idempotency key. An **attempt** is one
execution of a step, numbered from 1. A **result** is `Finished`, `Failed` or `Cancelled`, recorded
once. An **open step** is a step with no result. An **epoch** is one process lifetime of a run.

1. **One log is the truth.** `run.log` holds the inputs core has seen and the results of steps. A step
   with a result is never run again, and its result is reused. Core state is the fold of the log;
   `state.pb`, keyframes and `run.head` are caches.
2. **One writer per run.** The run's lock (`vs_runlog.RunLock`) is the only fence. A process that holds
   it is the only writer. Nothing else proves a process dead, and nothing needs to: the lock proves that
   no other process writes the log, and `clear` (rule 3) handles what the dead process left running.
3. **No result means run again.** Core never asks whether an open step happened. The runner of the
   step first calls `clear(tag)`, which removes or adopts what an earlier attempt left, and then runs the
   next attempt. The attempt number is recorded and shown.
4. **Pause is not a crash.** Pause stops starting steps, lets running steps finish or cancels them
   within a deadline, records that the run is parked, and exits. A clean pause leaves no open attempt.
   Ending a run cannot fail: cancel what is open, wait, then end.

## Status

| Part | Status |
|---|---|
| Core inputs as log records: `CoreInput`, `StepStarted`, `StepFinished`, `AttemptInterrupted`, and the confirmed append in `vs_runlog` | built, dormant (#3475 item 4a) |
| `RunStarted` and `CoreSnapshot` records, the seed of a fold | unbuilt; `fold` takes its seed as an argument until the cutover (PR 6a) |
| `fold(log)` and `decide(state, now) -> actions` in `vs_core` | built, dormant (#3475 item 4a) |
| Runner loop, `StepRunner` Protocol, `FakeStepRunner`, contract suite | built and dormant: `vs_runtime.api.step_runner` and `vs_runtime.api.testing`; no run selects it (#3475 item 4b) |
| Runners: agent turn and workspace | built, dormant: `vs_runtime.api.agent_step_runners`, with `vs_agent.reap_step_agents` and `AgentSessions.forget_unfinished` beneath them (#3475 item 5a); `DispatchTurn` only, `ResumeSessionTurn` and registered `SESSION_TURN` operations are not yet adapted |
| Runners: Slurm and local evaluation, registered operation, adoption | unbuilt; `libs/vs-slurm/src/vs_slurm/ledger.py` is the model |
| Pause drains within a deadline and records `Parked` | in progress (#3475 item 1) |
| `vs-dst` kills and pauses at every log write and every await | in progress (#3475 item 2) |
| `state.pb` as a cache, one-append commit | unbuilt |
| Prove-or-block layer ([inventory](#consumer-inventory)) | built; deleted at the cutover |
| Keyed replay, parts 1 to 7a | merged and dormant: no run selects it |
| Keyed replay, parts 7b to 9 | not built, and not needed by this design |

## Consumer inventory

Line counts are function counts from the syntax tree at commit `6cb578797`, rounded to what a reader can
act on. "Delete" means the behavior ends. "Move" means the behavior stays and its code is rewritten at the
named home, smaller. "Keep" means the code stays as it is.

### The layer is wider than its file list

The issue lists `_request_recovery`, `_attempt_retirement`, `_proofs` and `_settlement`. Reading the
callers changes that list in three ways.

- `_attempt_retirement.py`, `_continuations.py` and `_settlement.py` are mostly the live path
  (attempt closure, wait-all suspension, assessment and settlement). Only their release-proof helpers
  belong to the layer: 691, 96 and 14 lines.
- `_proofs.py` is a verdict algebra (`Proven`, `Missing`, `Mismatch`) imported by 27 core modules for
  ordinary request and observation correlation. Only its release proofs (207 lines) are the layer. Its
  stop, drain, admission and dependency rules (211 lines) are live and move.
- The pattern "an observation is `UNKNOWN`, so send an inspection" is woven into the live reducers:
  `_attempt_acquisition.py:_inspection`, `_session_inputs.py:_inspection`,
  `_session_scope.py:inspect_turn`, `_session_state.py:_inspect_session`, three sites in
  `_continuations.py` and two in `_attempt_retirement.py`. A file-by-file deletion leaves these behind.
  The fix removes the value (`UNKNOWN`) and the five inspection kinds, so the compiler finds every site.

### vs-core

Paths are under `libs/vs-core/src/vs_core/`.

| Module | Lines | Callers | Verdict |
|---|---|---|---|
| `_request_recovery.py` | 1,962 | `records.py` routes `RecoveryStarted`, `RecoveryReady`, `ReconciliationDeadline`, `ReconciliationCommandLapsed`, `RecoveryCheckWaitLapsed` and `open_host_epoch` to it; `_request_ledger.py` (`resent`), `_projection.py` (`next_reconciliation_at`), `scheduling.py` (`overdue_reconciliations`), `_quota_pause_clock.py` and `_watches.py` (`is_open`) | Delete 1,494: recovery barrier, child ownership, inspections, `_settle_unanswered`. Move 468: ending an open request at its limit (`_deadline`, `_lapse_command`, `_end_open_record`), cancelling the jobs of an ended submission (`_orphaned_jobs`), `open_host_epoch`'s downtime credit, `is_open` and the reconciliation timers. The move is a rewrite: a limit that passes becomes a `Cancel` action (see [Interfaces](#interfaces)) and the ordinary observation path |
| `_recovery_owners.py` | 232 | `_request_recovery.py` only | Delete: it decides which area owns a resource a request reported, which only reattachment needs |
| `_recovery_unanswered.py` | 85 | `_request_recovery.py` only | Delete: its four verdicts (`resend`, `unknown`, `terminal`, `blocked`) collapse to one, `resend` |
| `_proofs.py` | 1,120 | 27 core modules, `api/proofs.py`, `api/_exports.py` | Delete 207 (`resolved_observation`, `released_owner`, `_release_*`, `_released_*`, `blocked_unnamed`). Move 211 (`committed_stop`, `stop_mode`, `requested_stop_mode`, `draining`, `continues_admitted_work`, `admission_remaining`, `occupied_episode`, the dependency rules) to `_run_stop.py` and `_admission.py` as plain functions. Keep 702: correlation checks the live reducers use |
| `_attempt_retirement.py` | 1,941 | `attempts.py` only | Delete 691: release proofs, ownership discovery, `_inspect_unknown`, `_session_reattached`, `_reacquired`. Keep 1,250: closure, retention, guarded scope reopening. Closure waits for the scope's steps to have results instead of proving release |
| `_continuations.py` | 1,757 | `_step.py`, `_session_turns.py`, `_evaluation_history.py`, `evaluation.py`, `flows/_replay_turns.py` | Delete 96 (`_release_complete`, `_job_released`, `_descendants` and kin). Keep 1,661, including `credit_downtime` and `reach_deadlines` (wait deadlines) |
| `_settlement.py` | 505 | `settlement.py` only | Delete 14 (`_released`, the gate that publishes only after release proof). Keep 491 |
| `_inspection.py` | 217 | `_step.py` | Delete: correlates inspection answers, which no longer exist |
| `_host_epochs.py` | 69 | `records.py`, `_step.py`, `_wait_clock.py`, `_request_recovery.py` | Delete: the fold's clock is the recorded run clock, which excludes downtime, so no host log is needed |
| `_ownership.py` | 150 | `_step.py`, `_continuations.py` | Delete: the "cleanup fence" before terminal publication becomes the `End` rule of `decide` |
| `_job_releases.py` | 73 | `evaluation.py` | Delete: release proof of a closing attempt's jobs |
| `_waits.py` | 1,035 | `_step.py`, `_request_recovery.py`, runtime loop | Delete about 100 (recovery-check and blocked-record waits). Keep the orphan-wait check: it is rule "no orphan waits" of the functional core |
| `_request_ledger.py` | 1,054 | `records.py` | Delete about 270 (inspected-target observation, blocked-close pairing, `_reissuable` proofs, released-turn pairing). Keep the rest, including retry and reissue: the ledger already re-dispatches a reconciling `QUERY` or `IDEMPOTENT_WRITE` request (`_REPLAYABLE`) |
| `types/records.py` | 1,001 | everything | Delete about 310: `ChildLease`, `RecoveryPhase`, `RecoveryCheck`, `RecoveryBarrier`, `HostEpoch`, the recovery events, `InspectRequest`, `CancelOwnedResource`, `BlockRequest`. Also delete `InspectTurn` and `InspectOwnedJob` in `types/sessions.py` and `types/evaluation.py`, and `ObservationStatus.UNKNOWN` |
| `_step.py`, `_failure_routing.py`, `_parked_admission.py`, `_run_park.py`, `_run_pause.py`, `_dispatch_episode.py`, `_wait_clock.py`, `_turn_holds.py`, `_quota_pause_clock.py`, `_deadlines.py` | 1,961 and smaller | the reducer and its live rules | Keep. They lose the branches that call the deleted modules and the five recovery rows of `_failure_routing.py` |

### vs-runtime

Paths are under `libs/vs-runtime/src/vs_runtime/`.

| Module | Lines | Callers | Verdict |
|---|---|---|---|
| `_effect_fence.py` | 365 | `_evaluation_requests.py`, `_operation_requests.py`, `_observation_factory.py`, `_session_requests.py`, `_session_lifecycle_requests.py`, `_workspace_requests.py`, `_workspace_receipts.py`, `_replay_owner.py`, `_replay_operations.py`, `_replay_sessions.py`, `api/core.py`, `api/retained.py`, `api/fence_contracts.py` | Delete. `NothingHeld` and `MayHold` exist to prove "nothing is held"; `clear` replaces them |
| `_receipt_store.py` | 486 | 27 modules; `run_once` is used by 11 executors | Delete 265: `run_once`, `authorize`, `history`, `seal`, the `Settled`/`Transient`/`Replayed` result types. Keep 220: `exclusive`, `load`, `record_once`, `replace`, `modify`, the crash-safe keyed document store behind workspace receipts, operation receipts and the evidence ledger |
| `_access_settlement.py` | 377 | `_session_requests.py`, `_session_lifecycle_requests.py`, `_replay_sessions.py`, `api/core.py`, `api/retained.py` | Delete `access_unproven`. Keep the rest. `begin` and `settle` enforce the role's workspace grant and revert unauthorized writes, which is policy, not recovery. The runners also use `fence`, `receipt`, `restore_interrupted` and the new `rewind`, which `clear` of a turn calls to return the workspace to the baseline its receipt retains and reopen the key for the next attempt. `begin` calls `require_clear` and `blocking`, and the workspace's snapshot guard calls `fenced_by`, so the unsettled-writer index stays too: the 103 lines this row first counted for deletion are not removable without redesigning that guard |
| `_evaluation_recovery.py` | 311 | `_polling_evaluation.py` and `_semantic_slurm_evaluation.py` take it as an optional argument that no production wiring passes; `_replay_evaluation_jobs.py`; `api/slurm_evaluation_stack.py` | Delete |
| `_writer_reconciliation.py` | 392 | `_replay_*` (5 modules), `_run_environment.py` (`reconcile_writers` on four environments), `_evaluation_recovery.py` | Delete, with `reconcile_writers` and the no-op recovery mixins in `_run_environment.py` |
| `_core_loop.py`, `_core_run.py`, `_core_preflight.py`, `_core_wiring.py` | 1,620, 751, 220, 217 | `src/vibesys/run/core_run.py`, `core_services.py`, `vs_dst` | Replace with the runner loop (about 600 lines). Recovery start, `HostRenewed`, lease upkeep, `_recovery_cannot_open` and `_blocked_for_good` go; dispatch, publication delivery and settle move into the loop |
| `_replay_*.py` (17 modules), `api/replay.py` | 4,322 and 132 | tests, `src/vibesys/serial_replay.py` (173 lines, used only by `tests/vibesys/test_serial_replay.py`) | Delete. No production code selects replay. The one piece worth keeping is the log-append discipline in `_replay_store.py:RunLogReplayStore`, which moves to `vs_runlog` |

### vs-project

| Module | Lines | Callers | Verdict |
|---|---|---|---|
| `_store_operations.py` | 443 | `_state_store.py`, `_core_loop.py` (`acquire`, `renew`, `release`, `verify`, `mutate_if_fenced`, `commit`), `_receipt_store.py:authorize` | Delete: the lease, the fence and the compare-and-swap record. The run's lock replaces the lease. `state.pb` is written by the one writer with an atomic rename, never compared and swapped |
| `_state_store.py`, `api/state_store.py` | 314, 208 | `project.py`, `vs_dst/_snapshots.py`, `src/vibesys/run/core_services.py`, `src/launch`, `src/vibesys/api/assembly.py` | Delete with it |

### Tests

About 9,700 lines of `libs/vs-runtime/tests/test_replay_*`, `test_writer_*`, `test_keyed_runtime.py` and related files, and the `libs/vs-core/tests` files named `test_recovery_*`, `test_proof_*`, `test_request_recovery*.py`, `test_attempt_*_proofs.py` and kin, go with their subjects. Tests of live behavior that happen to drive recovery (for example `test_attempt_retirement.py`) are rewritten against the new rule, not deleted.

### Quota, capacity, stop and pause handling that must move

These behaviors live in the files above and stay, so deleting the files is not enough.

| Behavior | Where it lives today | Where it goes |
|---|---|---|
| A request ends at its limit with a typed reason (`TIMED_OUT`, `QUEUE_BUDGET_SPENT`) and its flight is cancelled | `_request_recovery.py:_deadline` (122 lines), fused with the recovery barrier | `decide` returns `Cancel(tag, DEADLINE)`; the result is an ordinary `Cancelled` observation. `_wait_clock.py` keeps the queue-budget rule |
| Jobs of an ended submission are cancelled | `_request_recovery.py:_orphaned_jobs`, `_orphan_source` | Same `Cancel` path: when a submission step gets a result, `decide` cancels its open job steps |
| Host downtime does not spend a limit | `_request_recovery.py:open_host_epoch` shifts every open limit | Not needed: the run clock is monotone across epochs and excludes downtime (`Record.clock`); a property test in the fold PR checks it |
| A quota pause does not spend a turn's limit | `_quota_pause_clock.py` imports `is_open` from `_request_recovery.py` | Keep; repoint the import to the new home of `is_open` |
| A capacity-gate hold does not spend a turn's limit | `_turn_holds.py`, `_proofs.py:occupied_episode` | Keep `_turn_holds.py`; move `occupied_episode` |
| Stop modes (drain, cancel), what a pause holds, admission budget | `_proofs.py` (`stop_mode`, `draining`, `continues_admitted_work`, `admission_remaining`) | `_run_stop.py` and `_admission.py` |
| Workspace grant enforcement | `_access_settlement.py:AccessSettlement.begin`, `settle` | Stays; becomes part of the agent-turn runner's `run` |
| Attempt retry bound and backoff | `_request_ledger.py:retry_delay`, `_exhausted`, `due_retries`, `Limits.request_retries` | Keep. Re-run attempts after a restart spend the same bound as retryable failures, so a step that kills its process every time ends as a typed failure |
| Pause and park admission | `_run_park.py` (278), `_run_pause.py` (269), `_parked_admission.py` (80), runtime `_bounded_stop.py` (254) | Keep; the pause change in #3475 item 1 owns them |

The issue's target of under 1,000 lines for "recovery, pause and fence code" cannot hold as written:
the pause files above are 881 lines of live behavior. This page measures the target on recovery and fence
code only (the decide rule, the loop, the log append), which is about 1,000 lines.

## Steps not safe to run twice

Core already classifies every request with `LifecycleClass`
(`libs/vs-core/src/vs_core/types/records.py:request_lifecycle`): `QUERY`, `IDEMPOTENT_WRITE`,
`OWNED_JOB` and `SESSION_TURN`. Recovery already settles an open `QUERY` or `IDEMPOTENT_WRITE` request as
`resend` (`libs/vs-core/src/vs_core/_recovery_unanswered.py:settlement`), and blocks every `OWNED_JOB` and
`SESSION_TURN`. It also blocks the first two when the record already completed but nothing proves who
owns the resource it reported, when it left a resource nobody proved released, or when its executor was
unreachable. The first case is #3458, a completed `EnsureSession` whose session ended without
acceptance. The table below marks `EnsureSession` safe to run twice. It keeps the four classes and adds
what `clear` does for each.

| Steps | Effect | After a crash | `clear(tag)` | Key |
|---|---|---|---|---|
| `ObserveOwnedJob`, `CollectEvidence`, `VerifyAdoption`; registered `QUERY` operations (`interpret_evidence`, `verify_revision`, board reads) | read only | safe | none | none |
| `EnsureWorkspace`, `RestoreRevision` | create or reset an attempt's workspace to a recorded revision | safe: the end state is fixed by the request | none | tag (workspace is keyed by attempt and generation) |
| `DiscardWorkspace`, `CloseAttemptScope`, `CloseSession`, `CancelTurn`, `CancelOwnedJob`, `EnsureSession` | release or bind a resource | safe: releasing twice is a no-op in the contract | none | tag |
| `SnapshotAndRetain`, `SnapshotAndRetainRun`, `RetainRevision` | commit the workspace and write a retention ref | safe after `clear` of the scope's writers, so the snapshot cannot race a running agent; the ref is content-addressed | reap tagged agent processes of the scope | revision id |
| `AdoptRevision` | write the winner into the operator's checkout | safe: it sets the target state and verifies it | none | tag |
| `DispatchTurn`, `ResumeSessionTurn`; registered `SESSION_TURN` operations | one agent turn: a process group, writes to the workspace, tokens, tool calls | safe after `clear` only | kill the processes carrying the tag in the run's containers (`vs_agent.reap_step_agents`; the containers belong to a role or a workspace and outlive the step, so none is removed), then, unless the journal holds an answer for the invocation, return the workspace to the baseline of its access receipt (`AccessSettlement.rewind`) and forget the invocation's unanswered journal record (`AgentSessions.forget_unfinished`), so the retry dispatches it again and resumes the last completed turn | tag |
| `SubmitMeasurement`; registered `OWNED_JOB` operations | an evaluation job on Slurm, Docker or a local process | Slurm: safe, the ledger finds the job by key. Docker and local: safe after `clear` | Slurm: adopt (job running or ended) or cancel by ledger key. Local and Docker: kill the stage process group, remove the labelled container, discard partial stage output | evaluation request digest plus tag |
| registered `IDEMPOTENT_WRITE` operations: `render_artifacts`, `retain_revision`, `export_patch`, the dynamic handoff write, issue-board `ApplyIssueChanges` and `LogProgress` | content-addressed or keyed writes | safe: each carries a key the tracker or store checks before writing | none | existing key |
| `capture_command`, `validate_local` | run a command or validation recipe in a workspace | safe after `clear` | kill the process group, reset the workspace to the recorded commit | tag |

`InspectRequest`, `InspectTurn`, `InspectOwnedJob`, `CancelOwnedResource` and `BlockRequest` are not in the
table because they exist only to settle unknown outcomes. They are deleted.

### What fits none

- **Effects an agent performs itself** outside the workspace and the tool servers, such as a network call
  or a push from a shell tool. A re-run repeats them and no key can guard them. Agent confinement
  ([sandboxing.md](sandboxing.md)) bounds what a turn can reach; that bound is the only protection.
- **Descendants that escape the process group and the marker** (a `setsid` child). `clear` cannot prove
  none remain. A Docker run environment removes the container, which ends them. The Host run environment
  has no such boundary, so its `clear` kills the recorded group and sweeps for the marker, and a ghost write
  is caught by `AccessSettlement.settle` before a result is accepted. This is a named residual, not a proof.
- **Usage of a killed attempt.** The provider may not report it. It is recorded as unknown, never zero.
- **A backend that offers neither a client-chosen id nor a list by label.** No `clear` can exist for it,
  so it cannot pass the contract suite and cannot be added as a step runner.

A step kind that is not safe to run twice is enforced by type in two places. `StepRunner.run` takes the
tag as a required argument, so a runner cannot start work with no key. Registration of an operation
(`libs/vs-core/src/vs_core/_registry.py:_validate_operation`) requires, for every non-`QUERY`
descriptor, that its runner is named, and the registry test runs each registered runner through the
contract suite, which replaces today's rule that such a descriptor declare `inspect`, `cancel` and `watch`.

## Is core state a fold of the log

No, not today, and the reason is not a missing field. `run.log` today holds a display projection of the
run (spans, metrics, lifecycle, intents, notices) written by a tee over the event journal, plus the
dormant keyed-replay records. It holds none of the inputs core consumed. Core state is a deterministic
reducer (`libs/vs-core/src/vs_core/_step.py:step`, from a state and an event to a state and requests),
and the only copy of its history is the latest `RunEnvelope` in `store.json` (schema version 42),
written by compare-and-swap.

Field by field (`libs/vs-core/src/vs_core/types/kernel.py:CoreState`, `RunEnvelope` and
`libs/vs-runtime/src/vs_runtime/_core_record.py:RuntimeRecord`):

| State | Where it lives today | Rebuildable from the log |
|---|---|---|
| `run` (facts, declaration, limits, requirements, capabilities, deadline, duration) | envelope; `EpochStarted.run` holds a display subset | No. Needs a `RunStarted` record of the inputs `new_core_state` takes |
| `run.controls`, `run.receipts`, `run.result` | envelope | Controls: from `CoreInput` control records. Receipts and result: derived by the fold |
| `scheduling`, `attempts`, `sessions`, `evaluation`, `settlement` | envelope | Derived by the fold of decisions, step results, controls and clock marks |
| `records.records` (the request ledger) | envelope | Derived. `phase` collapses to open or finished; attempts come from `StepStarted` |
| `records.recovery`, `records.hosts`, `records.children` | envelope | Not needed: the recovery barrier, the host epoch log and child leases are deleted |
| `strategy` and `event_cursor` | envelope | Derived: strategy state is a fold of the `StrategyEvent`s the fold emits, restored as today by replaying them |
| `fence` | envelope | Replaced by the run lock |
| `pending_inputs`, `pending_decisions`, `wakes`, `pending_publications`, `delivery_cursor` | runtime record | Derived: an owner event is a pure translation of its observation (`_request_forward.py`); decisions and wakes are the strategy's output; publications are the fold's output. The delivery cursor is the one fact that is not derived: after a restart every publication since the last recorded acknowledgement is delivered again |

Outside both stores, executors keep durable state keyed by tag: the provider session ids
(`vs_agent/session_store.py`), the Slurm ledger on the cluster, workspace git state, and the receipt-store
documents. None of it is core state. A runner reads it only inside `clear` and `run`.

### What the fold needs

A fold is only as good as three conditions, and the design states each.

1. **Determinism.** `step` and the strategy must give the same output for the same inputs. The strategy
   code is restricted by `scripts/check_purity.py` and `tests/architecture/test_flow_code_is_restricted.py`,
   and `vs_dst` raises `NondeterministicWorldError` when a world is not deterministic. The fold PR adds a
   check that folding a recorded log twice gives equal states.
2. **Same code.** A log folds only under the code that wrote it. A resume on changed code keeps today's
   rule: it continues at the next round with the ledger and best result carried over, and does not
   restore mid-round state. The log records the tree hash per epoch (`EpochStarted`), so the loop can name
   the divergence. Resume across a code change that reorders a flow is out of scope here.
3. **Complete inputs.** Every external input is a record.

New records (one proto envelope, `vibesys/store/v1/core_input.proto`, validated with protovalidate):

| Record | Carries | Why |
|---|---|---|
| `RunStarted` | the inputs `new_core_state` takes | seed of the fold |
| `CoreInput` | a decision or proposal, an owner event, an operator control, an epoch resume, a clock advance that changed state | the external inputs of `step` |
| `StepStarted` | tag, attempt, request, deadline | durable intent before any I/O; makes a re-run visible |
| `StepFinished` | tag, attempt, `Finished`, `Failed` or `Cancelled` with a typed reason | the result; one per step |
| `AttemptInterrupted` | tag, attempt, reason (`PAUSE`, `SHUTDOWN`, `RAISED`) | an attempt that ended without ending the step; `RAISED` is a runner that raised, the one reason that is charged to the retry bound. The loop writes `PAUSE` and `RAISED`; an orderly stop of the process (`SHUTDOWN`) is not built, and a cancelled loop is a crash |
| `CoreSnapshot` | an old `RunEnvelope` | the seed of a migrated or imported run only; never written for a new run |

The bodies of decisions, requests and observations are the canonical encoding of today's validated
`CoreEvent` and `Request` values in a bytes field with a schema version, the same interim form
`reply_json` and `LegacyEvent` already use. Core has proto types for ids, scopes, limits, controls and a
few leaf values (`proto/vibesys/core/v1`). Generating the 24 request kinds and the event kinds (about 200
`kind` literals in `vs_core/types`) is the phase 3 work of [run-transport.md](run-transport.md), not a
prerequisite of this design. A migration from
an existing run's `store.json` writes one `CoreSnapshot` record holding the old envelope as the seed of
the fold, with its recovery fields dropped and its open requests becoming open steps. The migration code
is deleted seven days after it ships.

### state.pb is a cache

[run-transport.md](run-transport.md) describes a commit as "append the log records, then compare-and-swap
`state.pb`, and on start drop log records past `state.pb`". That makes the log the lesser fact. This design
reverses it, and the run-transport page changes to match when the cutover lands.

- A commit is one append of one atomic record (`vs_runlog` already frames, checksums and drops a torn
  tail; `RunLogReplayStore` is the precedent). There is no second write to wait for.
- `state.pb` is written at pause, at end and at keyframes with a temp file, an fsync and a rename. It
  records the log sequence it covers. It is never compared and swapped and never authoritative.
- On start the loop reads `state.pb` if it parses and its sequence is not past the log; it then folds the
  records after that sequence. If it does not parse or is ahead of the log, the loop folds the whole log.
  Deleting `state.pb`, keyframes and `run.head` changes no result.
- `committed_seq` in `run.head` keeps its meaning: the gateway serves records up to the last durable one.

A cost to measure in the fold PR: a fold over the whole log grows with run length. The cache bounds it to
the records since the last keyframe; a property test fixes that a cached start and a full fold give equal
states.

## Interfaces

### vs-core: fold and decide

`vs_core.api.recovery` (new; pure; no I/O library import).

```python
@dataclass(frozen=True)
class Run:
    step: Request
    tag: RequestId
    attempt: int            # 1, or n + 1 after n attempts ended without a result
    deadline_at: float      # run-clock seconds; the loop sets a timer for it

@dataclass(frozen=True)
class Cancel:
    tag: RequestId
    reason: CancelReason    # DEADLINE, PAUSE_NOW, DRAIN_EXPIRED, RUN_ENDING, WITHDRAWN

@dataclass(frozen=True)
class Settle:
    tag: RequestId
    result: StepResult      # a result core decided without running: attempts spent, on_interrupt=CANCEL

@dataclass(frozen=True)
class Wait:
    until: float | None     # next deadline on the run clock, or None to wake on any input

@dataclass(frozen=True)
class End:
    result: RunResultProposal

type Action = Run | Cancel | Settle | Wait | End

class CancelReason(StrEnum):   # DEADLINE, PAUSE_NOW, DRAIN_EXPIRED, RUN_ENDING, WITHDRAWN, INTERRUPTED
    ...

type StepResult = Finished | Failed | Cancelled   # Failed carries a FailureReason, Cancelled a CancelReason

def fold(
    records: Iterable[Record], seed: CoreState | None = None, *, registry: OperationRegistry | None = None
) -> CoreState: ...
def decide(state: CoreState, now: float) -> tuple[Action, ...]: ...
```

`registry` decodes the outcome of a registered operation (`bind_outcome` needs it), which a bare
`CoreEvent` cannot. Without a seed or a `RunStarted` record a fold has no state to start from, so `fold`
raises; `RunStarted` is built with the cutover. `CancelReason` is a `StrEnum` in `vs_core`, and the record
carries the same set as the proto enum `StepCancelReason`, because the store package cannot import the core
package (a cycle); a test pins that the two name the same members. `Finished` carries the observation and,
when the executor reported more (a turn's reply, evidence, a revision), the whole `RequestObserved`.
`FailureReason` gains `ATTEMPTS_EXHAUSTED`.

`decide(fold(log), now)` is the issue's `decide(log) -> actions`. It is split in two so a cached state can
stand in for the fold. The action type has no `Blocked` or `Unknown` variant, and no variant names a step
kind. The rule is:

- an open step with no running attempt, whose dependencies hold and whose run may start steps, is `Run`,
  with `attempt = 1 + the latest attempt that began`; its deadline is the step's own limit, counted from
  its first start, and a later attempt keeps it;
- a running attempt past its deadline is `Cancel(DEADLINE)`; an open step with no running attempt past its
  deadline is `Settle(Cancelled(DEADLINE))`, because there is nothing to cancel;
- an open step whose attempts are spent is `Settle(Failed(ATTEMPTS_EXHAUSTED))`: attempts that began, less
  the ones an `AttemptInterrupted` record ended, exceed the step's retry limit (`retry_limit` of the
  request, else `Limits.request_retries.limit`);
- an open step marked `on_interrupt = CANCEL` whose attempt ended without a result, by an interruption
  record or by its process, is `Settle(Cancelled(INTERRUPTED))`;
- an open step whose dependency can never succeed is `Settle(Cancelled(WITHDRAWN))`; one whose dependency is
  still pending waits;
- a paused run (`PAUSED`, `DRAINING`, `PARKED`) starts no step except, while `DRAINING`, work the drain
  admits (`_run_park.dispatch_allowed`); a running step is `Cancel(PAUSE_NOW)` when Pause now withdrew it
  and `Cancel(DRAIN_EXPIRED)` from the pause's drain deadline (`RunPark.deadline_at`);
- a run whose committed finish is a cancel, or that has ended, gets `Cancel(RUN_ENDING)` for each running
  step and `Settle(Cancelled(RUN_ENDING))` for each other open step, and starts none;
- a run with a committed finish and no open step is `End`.

A step runs now when its latest attempt began in the epoch the log is in and no interruption record ended
it. The epoch of the log is the largest `Record.epoch` the fold has seen, so an attempt of an earlier epoch
ended with its process. Cleanup steps that core issues while a cancelling finish is committed are cancelled
like any other; whether cleanup should instead outlive the finish is left to the cutover.

`decide` is total: it never raises on a validated log, and a log that fails validation is a read error at
the boundary (a corrupt record stops the reader, as `vs_runlog` does today), never an action.

### vs-runtime: runner loop and StepRunner

`vs_runtime.api` owns the Protocol, because `vs_runtime` already owns the seam where core requests meet
I/O libraries (`_core_requests.py:ExecutorRole`, `RequestExecutors`) and the libraries below it cannot
import it. Each runner is a `vs_runtime` implementation that adapts one library's own typed interface
(`vs_agent`, `vs_slurm`, `vs_project`). The library supplies the mechanism (kill by tag, adopt by ledger
key); the issue's wording "one `StepRunner` each in `vs-agent`, `vs-slurm`" would need an upward edge.
Implementations are named `<Variant><Role>`: `FakeStepRunner`, `AgentTurnStepRunner`,
`WorkspaceStepRunner`, `SlurmJobStepRunner`, `LocalJobStepRunner`, `OperationStepRunner` and
`AdoptionStepRunner`.

```python
@dataclass(frozen=True)
class Adopted:
    result: StepResult | None   # None: the step is still running; the next run attaches to it

@dataclass(frozen=True)
class Cleared:
    pass

class StepRunner(Protocol):
    async def run(
        self, step: Request, tag: RequestId, attempt: int, cancel: CancelSignal
    ) -> StepResult: ...

    async def clear(self, tag: RequestId) -> Adopted | Cleared: ...
```

Contract, which every implementation passes in `vs_runtime.api.testing.StepRunnerContract`, a class a test
module subclasses with one factory method, as the `vs_sim` contracts are:

1. `run` returns a result, or raises. A raise is "no result": the loop treats it like a crash.
2. After `clear`, a second `run` with the same tag and a higher attempt starts clean.
3. `clear` is idempotent, returns `Cleared` for a tag that never started, and returns only when nothing
   carrying the tag runs or will start.
4. `Adopted` means the runner took over what an earlier attempt left. With `result` set the step had
   ended, and `run` returns that result. With `result` unset the work is still going, and `run` attaches
   to it instead of starting new work. Agent turns never adopt. Slurm jobs adopt, because they survive
   the host by design.
5. After `cancel` is set, `run` returns `Cancelled` within its grace period, or the loop cancels the task
   and calls `clear`.
6. `clear` may raise `ClearUnavailableError` when its backend cannot be reached. The loop retries with
   backoff, shows the run as waiting on that runner, and records no result.

The suite runs against `FakeStepRunner` in fast tests and against the real runners in the Slurm and
end-to-end tiers. Each real runner also passes the kill-mid-run and same-tag-twice cases; the Fake is
never more forgiving than a real one.

The loop, `vs_runtime/_step_loop.py`:

```text
state = fold(log)                      # cache plus suffix, or the whole log
repeat:
    for action in decide(state, clock.now()):
        Run:    if attempt > 1: await runner.clear(tag)
                append StepStarted(tag, attempt)      # durable before I/O
                start runner.run(...) as a task
        Cancel: set the signal; on return append StepFinished or AttemptInterrupted
        Settle: await runner.clear(tag) if an attempt was interrupted; append StepFinished
        Wait:   await the first of: a task ending, a new input (a pause is one), the timer
        End:    append Ended; release the lock
    state = fold(state, the records just appended)
```

The loop owns timers, tasks, cancellation and recording. `decide` owns every decision, including that a
deadline passed. No `if isinstance(step, ...)` appears in either.

In code the loop sees the world through two interfaces it owns. `StepLog` is the log as the loop uses it:
one atomic durable append per method, a `state` that is the fold of everything appended (by the loop or by
an input from elsewhere), and `latest_run(tag)`, the request of the last recorded attempt. `RecoveryRule`
is core's pure surface: `decide`, and `settled`, which says the log already holds the end of the run
(`ENDED`) or the run is paused (`PARKED`). A restarted loop over a log that holds `Ended` returns at once
instead of recording it twice. Each running attempt is a flight, one task that owns the attempt from
`clear` to the record of how it ended, so a `clear` that waits on an unreachable backend never blocks the
loop or an ending. How an attempt ends depends on why it was cancelled:

| Cancel reason | Recorded | `clear` unreachable |
|---|---|---|
| `PAUSE_NOW`, `DRAIN_EXPIRED` | `AttemptInterrupted(PAUSE)` | give up; the next epoch's `clear` handles the leftovers |
| `RUN_ENDING` | `StepFinished(Cancelled)` | give up; the leftovers are a named residual |
| `DEADLINE`, `WITHDRAWN`, `INTERRUPTED` | `StepFinished(Cancelled)` | keep retrying with backoff |

An attempt that returned a result on its own is recorded as that result even when a cancel was set. An
attempt the loop gave up on (it ignored the signal through the grace period, or raised) is cleared before
its end is recorded, so a paused or ended run leaves nothing running. A parked run starts nothing, so
flights still waiting on `clear` stop waiting. `Settle` and a `Cancel` with no flight (the host restarted)
run `clear` and record the result; if `clear` adopts work that is still going, the loop attaches to it with
the cancel signal set so that it ends. A raise out of `run` is "no result": the loop records
`AttemptInterrupted(RAISED)`, and the next `Run` is attempt n + 1 after a `clear`.

A re-run is shown from the same facts. The store already has a span `Attempt` message with `n` and `of`
(`proto/vibesys/store/v1/span.proto`). The journal projection sets `n` from `StepStarted.attempt`.
DECISION: add `ATTEMPT_OUTCOME_INTERRUPTED` to its outcome enum (an additive change), so a restart is
not drawn as a retry after a failure and the screen needs no wording parse. Showing that outcome on the Run
screen is a client change in the pull request that lands the records (PR 6a).

A result is accepted only for the current attempt. A late return from a cancelled task is dropped, because
the loop appended `AttemptInterrupted` first. With one writer per run this needs no fence.

### The step declares what an interruption means

Chat sessions are interactive: a person watches the turn, has seen partial output, and may prefer to
decide. Re-running a paid turn that someone already read can repeat what they saw.

DECISION: a step declares `on_interrupt: RERUN | CANCEL` as a typed field, default `RERUN`. The field is
on `TurnSpec` and on `OperationSchemaRef`, which `OperationDescriptor` extends and a registered request
carries, so `decide` reads it from the request without a registry lookup. The asker knows whether a human
is present and the runner does not. Non-turn steps cannot set `CANCEL`: a schema reference that is not a
session turn refuses it at construction, and every other request kind is `RERUN` by
`request_interrupt_policy`.
For `CANCEL`, an interrupted attempt is cleared and then settled as `Cancelled(INTERRUPTED)`, the chat UI
shows the interruption and offers a retry, and the retry is a new step with a new tag (t3code's behavior).
Search and evaluation steps keep `RERUN`: nobody reads their partial output, and they are the steps whose
loss costs the most.

### Pause

The pause change in #3475 item 1 supplies the drain: it stops `Run` for new steps, waits until each running
step ends or the drain deadline passes, records `Parked`, and exits. This design asks three things of
it. A step cancelled by Pause now is recorded as `AttemptInterrupted(PAUSE)`, not as a result, so Resume
runs it again as the next attempt. An interrupted attempt is neither a policy failure nor a retry debit,
as [run-transport.md](run-transport.md) already says. The drain deadline is a field of the pause input,
not a constant in the loop.

## Alternatives considered

One rubric, scored the same way for each. Guarantees: never stuck, each step finishes once in the log,
re-runs are visible. Cost: the work a new step kind adds. Trusted surface: what must be right for the
guarantees to hold. Migration: the work to get there from `6cb578797`.

| | (a) This design | (b) Finish keyed replay 7b to 9 | (c) Keyed replay, unknown and withdraw replaced by re-run | (d) Settle open steps as interrupted at restart |
|---|---|---|---|---|
| Level | the core request ledger and the shell | the flow, over keyed spans | the flow, over keyed spans | the strategy decides |
| Never stuck | yes: no blocked variant; an unreachable backend waits visibly and the run can still be ended | no: `ReplayUnknown` and "Recovery blocked" stay by design, and Host, Modal and Slurm return `Unsupported` | yes, for `single` only | yes |
| Each step finishes once in the log | yes: one `StepFinished` per tag | yes | yes | no: the step fails and the strategy starts another, so one unit of work has several results |
| Re-runs visible | yes: `StepStarted.attempt` | n/a: it blocks instead | yes | no: appears as a failure then a new step |
| Strategies covered | all five (the kernel runs every strategy) | `single` only; the other four stay positional | `single` only | all five |
| Does it fix the reported failure | yes (a `dynamic` planner session) | no: `dynamic` is not ported | no, same reason | yes |
| Cost of a new step kind | one runner (`run`, `clear`) and a contract run; no core change. Today a new kind touches the proof, owner, recovery and retirement modules | a keyed owner with `execute` and `inspect`, a classified context and proto messages per kind | an owner with `run` and `clear`, plus the classified context | a run-wide reap; strategies must treat `Interrupted` as no debit, in each strategy |
| Trusted surface | each runner's `clear`; `decide` is small | writer proof per environment (Docker only), identity encoder, archive validator | identity encoder, archive validator, each `clear` | the run-wide reap, and five strategies' failure handling |
| Migration | rewrites the loop (about 2,800 lines replaced by about 600) and slims the ledger; deletes about 5,700 lines on the production path | 5,200 to 8,150 changed lines plus 250 to 450 deleted (the estimate in #3408), high risk | smaller than (b), but the kernel's recovery layer stays for the four peers, so the layer cannot be deleted | smallest, but rule 3 and the "attempt greater than 1" criterion are unmet |

Reasons for the choice:

- (b) cannot meet the first guarantee, because it blocks whenever it cannot prove writers dead, and it covers
  one strategy of five.
- (c) keeps the guarantee but only for `single`. The recovery layer in the kernel serves the other four,
  so (c) leaves two recovery systems and cannot delete the layer, which is the stated acceptance
  criterion.
- (d) is the cheapest, and it is wrong in a way the rubric shows: a hypothesis marked failed because the
  host restarted corrupts the search, and "interrupted is not a failure" would have to be implemented in
  every strategy. It is also the one place where re-runs are not visible as attempts.
- An external durable-execution service, a fifth level, is not scored: it adds a server dependency
  to a design whose first principle is that the run process only writes files
  ([run-transport.md](run-transport.md)).

DECISION: (a), at the kernel level. This is the largest judgment in this page. It differs from the issue
in one respect: the issue reads as if the flow were the level ("core knows nothing about step kinds"). The
reducers do know step kinds (sessions, attempts, evaluation, settlement are policy), and moving that policy
out of core is not part of this effort. What knows no step kinds is the recovery rule.

## Sequencing and deletion list

Each pull request below is dormant until the cutover: test-reachable, not selectable by a run.

| PR | Write set | Deletes |
|---|---|---|
| 4a. Inputs and decide | `proto/vibesys/store/v1/core_input.proto` and its generated code; `libs/vs-runlog/src/vs_runlog/` (input append, read, the log-prefix rule moved from `_replay_store.py`); `libs/vs-core/src/vs_core/api/recovery.py` and `_decide.py` (`fold`, `decide`, `Action`); `libs/vs-core/tests` (properties over generated logs: fold twice equals once, cached start equals full fold, crash at every record prefix yields `Run` for every open step and never a blocked action) | nothing |
| 4b. Loop and Protocol | `libs/vs-runtime/src/vs_runtime/_step_loop.py`, `_step_runner.py`, `api/step_runner.py`, `api/step_runner_contracts.py`, `api/testing.py` (`FakeStepRunner`, `FakeStepLog`, `StepRunnerContract`); `tach.toml`; `libs/vs-dst` hooks (#3475 item 2 owns the points) | nothing |
| 5a. Agent turn and workspace runners | `libs/vs-runtime/src/vs_runtime/_agent_turn_step_runner.py`, `_workspace_step_runner.py`; `libs/vs-agent/src/vs_agent/orphans.py` gains reap by step tag beside reap by run | nothing |
| 5b. Evaluation runners | `libs/vs-runtime/src/vs_runtime/_slurm_job_step_runner.py`, `_local_job_step_runner.py`; Slurm tier case | nothing |
| 5c. Operation, adoption and environment runners | `libs/vs-runtime/src/vs_runtime/_operation_step_runner.py`, `_adoption_step_runner.py`; per-environment `clear` in `_run_environment.py` (Docker, SkyPilot, Modal, Host) | nothing |
| 6a. Cutover | `src/vibesys/run/core_run.py`, `core_services.py`, `src/launch`; `vs_runlog` `state.pb` cache writer; the one-time `CoreSnapshot` migration; `libs/vs-dst` runs the production loop | none yet; the old path is unreachable |
| 6b. Deletion and guard | the files in the [inventory](#consumer-inventory); `tests/architecture/test_no_prove_or_block.py`; `tach.toml`; this page's status table, [run-transport.md](run-transport.md) commit text | about 5,700 production lines plus about 5,700 dormant keyed lines, and their tests |

PR 6a and 6b land back to back, with no cleanup deferred, as #3408 required of its own production switch.

### What of keyed replay stays

- **Saved-result reuse stays as the ledger already does it.** A finished request is a `COMPLETED` record in
  the fold, and the shell dispatches only `PREPARED` records
  (`libs/vs-runtime/src/vs_runtime/_core_loop.py:CoreRuntime._authorized`). No keyed code is needed for it.
- **Kept, moved:** the log-append commit discipline of `RunLogReplayStore` (torn tail repaired, complete
  corruption fails, uncertain commit authorizes no dispatch), into `vs_runlog`.
- **Kept, dormant:** the pure core half of keyed replay in `libs/vs-core/src/vs_core/flows/`
  (`_replay_identity.py`, `_replay_causality.py`, `_replay_schema.py`, `_replay_history.py`,
  `_replay_sources.py`, `_replay_run.py`, `_replay_archive.py`, `_replay_accounting.py`,
  `_replay_resources.py`, `_replay_iterations.py`, `_replay_controls.py`, `_replay_turns.py`, and
  `api/replay.py`: about 3,900 lines). It is the only tested way to reuse results across a code edit by
  semantic key, which this design does not need and a later one may. DECISION: keep it for one review
  cycle, with no importer outside tests, and delete it if no consumer is scheduled. The orchestrator
  can choose to delete it in 6b instead; nothing else depends on it.
- **Deleted:** `_replay_physical.py` and `_replay_writers.py` (343 lines) and the archive's checks of
  them, the whole `vs_runtime` half (4,454 lines), `_writer_reconciliation.py`, `_evaluation_recovery.py`,
  `src/vibesys/serial_replay.py`, and the `ReplayUnknown`, retire and withdraw paths with them. Their
  protos are removed in 6b; `buf breaking` flags the removal, so 6b names an exception, valid because
  no run has written those messages.

### Keeping the deleted modules from coming back

`tests/architecture/test_no_prove_or_block.py` fails when any of these holds, in the strongest form each
allows (see the `enforce-rules` skill):

- A listed module path exists.
- A listed identifier (`RecoveryBarrier`, `RecoveryCheck`, `HostEpoch`, `ChildLease`, `InspectRequest`,
  `InspectTurn`, `InspectOwnedJob`, `CancelOwnedResource`, `BlockRequest`, `EffectFences`, `NothingHeld`,
  `MayHold`, `ReplayUnknown`, `StoreFence`, `HostFence`) is defined or imported in `src/` or `libs/*/src`.
- `ObservationStatus` has a member named `UNKNOWN`, or any member of `vs_core.api.recovery.Action` or
  `StepResult` has a `kind` containing `blocked` or `unknown`. This one is a type check: the closed union
  cannot represent the state.
- A `StepRunner` implementation is not listed in the contract suite's parametrization.

The first two are an AST scan like `tests/architecture/test_epoch_ending_authority.py`. Unenforced
remainder: the guard cannot tell a new mechanism with a different name that does the same job; review
applies the rule "no result means run again".

## Future features

| Feature | Verdict | Why |
|---|---|---|
| New compute backend | supports | One runner and one contract run. It needs a client-chosen id or a list by label; without one it cannot be added (see [what fits none](#what-fits-none)) |
| New agent provider | neutral | `clear` reaps by tag through agentshim's confinement, which is provider-independent. Provider knowledge stays in agentshim ([agent-drivers.md](agent-drivers.md)). The one provider-specific fact is whether a killed turn leaves a partial transcript that a resume would show: a contract case per provider |
| Chat sessions | supports | `on_interrupt = CANCEL` on the turn. A chat session is a process with its own log and today reuses the auxiliary chat agent ([run-transport.md](run-transport.md)), not the request ledger, so the declaration has no consumer until a chat turn is a step. It costs nothing to declare now |
| Multi-host runs | adds one site | The run lock is a local `flock` and assumes the writer and the log share a host (run-transport's open question on NFS). A second host needs a lock behind an interface. Runners also need `StepStarted` to record where the attempt ran, so `clear` reaches that host. The `StepRunner` contract itself does not change |
| New step kind | neutral | A runner and a contract run, no change to core |
| New strategy | neutral | Strategies see results and attempts, never a blocked state |
| Resume on edited code | neutral | Same rule as today (next round). The dormant keyed code is the path to more |
| Retained-run import | neutral | An imported run starts from a `CoreSnapshot` record, as a migrated one does |

## Open pull requests

#3458 changes `_recovery_owners.py:known_owner`, and #3451 changes
`libs/vs-runtime/src/vs_runtime/_replay_run.py:_LiveEpoch`. Both are
members of the class this page removes. #3458 becomes unnecessary: with no recovery check there is no
`known_owner`, and a session that ended without acceptance has a recorded result like any other step.
#3451 becomes unnecessary for production and stays correct for the dormant keyed runtime until PR 6b
deletes `_replay_run.py`. Neither should be reworked to fit this design. Merging them first costs nothing
beyond their review time, and neither blocks the sequence above.

## Open risks

These are facts the implementation must settle, found while checking the design against the code.

- **Re-run and limits.** A re-run attempt spends the step's retry bound
  (`Limits.request_retries`); an interruption by Pause does not.
  DECISION: a re-run turn does not count against any per-round limit. Turn, attempt and admission charges
  are made when a request is accepted, once per invocation, and a re-run is another attempt of the same
  invocation, so `StepStarted` and `AttemptInterrupted` change only the step's own record and the attempts
  the fold keeps. `libs/vs-core/tests/test_recovery_decide.py` pins it: folding a log with its re-runs,
  interruptions and restarts gives the same attempts, sessions, scheduling, evaluation and settlement state
  as folding it with them removed.
- **Partial transcripts.** Whether a killed provider turn leaves entries a resume would show depends on the
  provider. The checkpoint saved after each completed turn (`vs_agent/session_store.py`) is the safe point;
  a provider that cannot resume from it falls back to a fresh conversation from retained content, which the
  client already does for a standalone message.
- **Clear completeness on the Host run environment.** See [what fits none](#what-fits-none).
- **The turn runner still goes through the session executor's fences.** `RuntimeSessionRequests.attempt`
  skips the receipt of the result (`run_once`), but `_dispatch` still enters `EffectFences` and records a
  `DispatchRecord` before the provider call. They hold across a rerun because `clear` runs first, and the
  cutover removes them from `_dispatch` together with `_effect_fence.py`.
- **Provider session id of a killed turn.** `forget_unfinished` does not touch the provider conversation. A
  provider that rewrites its session id mid-turn makes the retry's expected-checkpoint check fail loudly
  (`SessionResumeError`) instead of resuming; the fix belongs to that provider's contract case.
- **Fold cost.** See [state.pb is a cache](#statepb-is-a-cache).
- **Interim bodies.** Request and event bodies in the new records are not yet proto messages. Until phase 3
  generates them, schema evolution of those bodies follows today's envelope migration chain, which is
  deleted at phase 3.
