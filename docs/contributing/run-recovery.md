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
| Core inputs as log records: `CoreInput`, `StepStarted`, `StepFinished`, `AttemptInterrupted`, and the confirmed append in `vs_runlog` | in use (#3475 item 4a) |
| `RunStarted` (the seed of a fold) and `StrategyAdvanced` (one advance of the search) records, `RunLogStepLog`, `CoreInputs`, `StepRouter`, one appender and one lock for the run's records | in use; also run in simulation by `vs_dst.StepLoopHosting` (PR 6a-2) |
| `CoreSnapshot` record, the seed of a migrated run, and `vibesys.run.store_migration`, the one-time migration of a run's `store.json` into it | in use (PR 6a-3m); the migration is deleted seven days after the cutover ships |
| `fold(log)` and `decide(state, now) -> actions` in `vs_core` | in use (#3475 item 4a) |
| Parked, draining, ended and resumed runs in the fold: the `RunEnded` record, `open_step`, the park advance (#3475, core gaps) | in use |
| Runner loop, `StepRunner` Protocol, `FakeStepRunner`, contract suite | in use: `vs_runtime.api.step_runner` and `vs_runtime.api.testing`; every run selects it (#3475 item 4b, PR 6a-3) |
| Runners: agent turn (`DispatchTurn`, `ResumeSessionTurn`, registered `SESSION_TURN` operations) and workspace | in use: `vs_runtime.api.agent_step_runners`, with `vs_agent.reap_step_agents` and `AgentSessions.forget_unfinished` beneath them (#3475 item 5a). [Which runner owns each request kind](#the-runner-of-each-request-kind) names the 13 kinds with no owner |
| Runners: Slurm and local evaluation (`SlurmJobStepRunner`, `LocalJobStepRunner`) | in use; the local runner reaps what a dead process left through a `StageWorkReaper`: `ProcessStageWorkReaper` ends the stage processes and `ContainerStageWorkReaper` the Docker candidate containers of the tag, both found by the step mark (see [Marks](#marks-on-local-stage-work)) |
| Runners: registered operation and adoption | in use: `vs_runtime.api.operation_step_runner` and `vs_runtime.api.adoption_step_runner`, each in the contract suite (#3475 item 5c) |
| Runner of the 13 executor kinds: `ExecutorStepRunner` | in use: `vs_runtime.api.executor_step_runner`, in the contract suite for every kind (#3475 item 5d) |
| Per-environment `clear` (Docker, SkyPilot, Modal, Slurm, Host) | in use: `RunEnvironment.agent_work_reaper` (#3475 item 5e); every environment clears an agent step, the Host environment on macOS through a leader that carries the mark, see [open risks](#open-risks) |
| Pause drains within a deadline and records `Parked` | in progress (#3475 item 1) |
| `vs-dst` kills and pauses at every log write and every await | built; `StepLoopHosting` runs the production loop for all five searches (PR 6a-2). Every kill resumes to the uninterrupted result, and so does every pause, and a resumed host paused again (`interrupt(point, then=...)`); a pause's drain lets the turns it admitted go on (`admitted_work_proceeds`) |
| `state.pb` as a cache of the fold: `FoldCache` in `vs_runlog`, the start from it and its writes in `RunLogStepLog` | in use (PR 6a-3m); the one-append commit is the log's `ConfirmedLog` |
| Prove-or-block layer ([inventory](#consumer-inventory)) | built; deleted at the cutover |
| Keyed replay, parts 1 to 7a | deleted (the `vs_runtime` driver, the `vs_core` flows and `api/replay.py`, `serial_replay.py` and their tests); the protos and `RunLogReplayStore` stay until 6b (see [what of keyed replay stays](#what-of-keyed-replay-stays)) |
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
| `_continuations.py` | 1,757 | `_step.py`, `_session_turns.py`, `_evaluation_history.py`, `evaluation.py` | Delete 96 (`_release_complete`, `_job_released`, `_descendants` and kin). Keep 1,661, including `credit_downtime` and `reach_deadlines` (wait deadlines) |
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
| `_effect_fence.py` | 365 | `_evaluation_requests.py`, `_operation_requests.py`, `_observation_factory.py`, `_session_requests.py`, `_session_lifecycle_requests.py`, `_workspace_requests.py`, `_workspace_receipts.py`, `api/core.py`, `api/retained.py`, `api/fence_contracts.py` | Delete. `NothingHeld` and `MayHold` exist to prove "nothing is held"; `clear` replaces them |
| `_receipt_store.py` | 486 | 27 modules; `run_once` is used by 11 executors | Delete 265: `run_once`, `authorize`, `history`, `seal`, the `Settled`/`Transient`/`Replayed` result types. Keep 220: `exclusive`, `load`, `record_once`, `replace`, `modify`, the crash-safe keyed document store behind workspace receipts, operation receipts and the evidence ledger |
| `_access_settlement.py` | 377 | `_session_requests.py`, `_session_lifecycle_requests.py`, `api/core.py`, `api/retained.py` | Delete `access_unproven`. Keep the rest. `begin` and `settle` enforce the role's workspace grant and revert unauthorized writes, which is policy, not recovery. The runners also use `fence`, `receipt`, `restore_interrupted` and the new `rewind`, which `clear` of a turn calls to return the workspace to the baseline its receipt retains and reopen the key for the next attempt. `begin` calls `require_clear` and `blocking`, and the workspace's snapshot guard calls `fenced_by`, so the unsettled-writer index stays too: the 103 lines this row first counted for deletion are not removable without redesigning that guard |
| `_evaluation_recovery.py` | 311 | `_polling_evaluation.py` and `_semantic_slurm_evaluation.py` take it as an optional argument that no production wiring passes; `api/slurm_evaluation_stack.py` | Delete |
| `_writer_reconciliation.py` | 326 | `_run_environment.py` (`reconcile_writers` on four environments), `_evaluation_recovery.py` | Delete, with `reconcile_writers` and the no-op recovery mixins in `_run_environment.py` |
| `_core_loop.py`, `_core_run.py`, `_core_preflight.py`, `_core_wiring.py` | 1,620, 751, 220, 217 | `src/vibesys/run/core_run.py`, `core_services.py`, `vs_dst` | Replace with the runner loop (about 600 lines). Recovery start, `HostRenewed`, lease upkeep, `_recovery_cannot_open` and `_blocked_for_good` go; dispatch, publication delivery and settle move into the loop |
| `libs/vs-runtime/src/vs_runtime/_replay_store.py`, `api/replay.py` | 167 and 33 | `_replay_store.py` has no importer but `api/replay.py`, which now exports it with the writer-proof and evaluation-recovery modules above | Delete both with the layer. `RunLogReplayStore` stays only until its log-append discipline, which item 4a moves to `vs_runlog.ConfirmedLog`, has no other user. Its contract suite (`libs/vs-runtime/tests/test_replay_store_contract.py`) drives the store directly |

### vs-project

| Module | Lines | Callers | Verdict |
|---|---|---|---|
| `_store_operations.py` | 443 | `_state_store.py`, `_core_loop.py` (`acquire`, `renew`, `release`, `verify`, `mutate_if_fenced`, `commit`), `_receipt_store.py:authorize` | Delete: the lease, the fence and the compare-and-swap record. The run's lock replaces the lease. `state.pb` is written by the one writer with an atomic rename, never compared and swapped |
| `_state_store.py`, `api/state_store.py` | 314, 208 | `project.py`, `vs_dst/_snapshots.py`, `src/vibesys/run/core_services.py`, `src/launch`, `src/vibesys/api/assembly.py` | Delete with it |

### Tests

About 1,600 lines of `libs/vs-runtime/tests/test_writer_*`, `test_local_evaluation_receipts.py` and related files, and the `libs/vs-core/tests` files named `test_recovery_*`, `test_proof_*`, `test_request_recovery*.py`, `test_attempt_*_proofs.py` and kin, go with their subjects. Tests of live behavior that happen to drive recovery (for example `test_attempt_retirement.py`) are rewritten against the new rule, not deleted.

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
| `ObserveOwnedJob`, `CollectEvidence`, `VerifyAdoption`; registered `QUERY` operations (`interpret_evidence`, `verify_revision`, `export_patch`, board reads) | read only | safe | none | none |
| `EnsureWorkspace`, `RestoreRevision` | create or reset an attempt's workspace to a recorded revision | safe: the end state is fixed by the request | none | tag (workspace is keyed by attempt and generation) |
| `DiscardWorkspace`, `CloseAttemptScope`, `CloseSession`, `CancelTurn`, `CancelOwnedJob`, `EnsureSession` | release or bind a resource | safe: releasing twice is a no-op in the contract | none | tag |
| `SnapshotAndRetain`, `SnapshotAndRetainRun`, `RetainRevision` | commit the workspace and write a retention ref | safe, and `decide` makes it safe: it runs the step only when every writer of its scope has a result, so it never reads a half-written tree ([Scope order](#scope-order)); the ref is content-addressed | none | revision id |
| `AdoptRevision` | write the winner into the operator's checkout | safe: it sets the target state and verifies it | none | tag |
| `DispatchTurn`, `ResumeSessionTurn`; registered `SESSION_TURN` operations | one agent turn: a process group, writes to the workspace, tokens, tool calls | safe after `clear` only | kill the processes carrying the tag in the run's containers (`vs_agent.reap_step_agents`; the containers belong to a role or a workspace and outlive the step, so none is removed), then, unless the journal holds an answer for the invocation, return the workspace to the baseline of its access receipt (`AccessSettlement.rewind`) and forget the invocation's unanswered journal record (`AgentSessions.forget_unfinished`), so the retry dispatches it again and resumes the last completed turn | tag |
| `SubmitMeasurement`; registered `OWNED_JOB` operations | an evaluation job on Slurm, Docker or a local process | Slurm: safe, the ledger finds the job. Docker and local: safe after `clear` | Slurm: adopt (request claimed, queued, running or ended); a pause or a drain deadline leaves the job running and `run` attaches to it; only a cancel that ends the step cancels it. Local and Docker: cancel the process's own evaluation, kill the stage process group, remove the labelled container, discard partial stage output | Slurm: the ledger's content key, which `clear` computes from the step. Local: the tag |
| registered `IDEMPOTENT_WRITE` operations: `render_artifacts`, `retain_revision`, the dynamic handoff write, issue-board `ApplyIssueChanges` and `LogProgress` | content-addressed or keyed writes | safe: each carries a key the tracker or store checks before writing | none | existing key |
| `capture_command`, `validate_local` | run a command or validation recipe in a workspace | safe after `clear` | kill the process group, reset the workspace to the recorded commit | tag |

The ledger keys a request by a hash of the content the job reads and its spec, not by the tag, so `clear(step, tag)` computes the key from the step: it opens a candidate worktree at the step's revision, hashes it, discards it, and reads the ledger. Two steps of one content therefore share one job, and a step that finds the job another step submitted attaches to it. A job that was cancelled (its cancel ended the step, or someone else cancelled it) is final in the ledger, so the next attempt asks for a new handle of the tag (`-life-n`) and the ledger claims a new job. A pause or a drain deadline cancels nothing: the job stays queued or running, `clear` adopts it and the next attempt attaches. A local evaluation never attaches: each run takes a fresh handle.

`InspectRequest`, `InspectTurn`, `InspectOwnedJob`, `CancelOwnedResource` and `BlockRequest` are not in the
table because they exist only to settle unknown outcomes. They are deleted.

### The runner of each request kind

Every kind of `libs/vs-core/src/vs_core/types/records.py:Request` (24, in six groups) has one runner, or
is deleted, or has no owner yet. A runner is named only if its module exists in the pull request that
lands it. "Executor step" below means a step that runs on its role's existing executor and needs no
`clear` (the "none" rows of the table above); `ExecutorStepRunner` runs them.

| Group | Kind | Runner | State |
|---|---|---|---|
| Session | `DispatchTurn` | `AgentTurnStepRunner` | in use (5a) |
| Session | `ResumeSessionTurn` | `AgentTurnStepRunner` | in use (5a) |
| Operation | `ExecuteRegisteredOperation` of lifecycle `SESSION_TURN` | `AgentTurnStepRunner`, through an `OperationTurns` the wiring supplies | in use (5a), wired by `vibesys.run.step_runners` (PR 6a-3) |
| Operation | `ExecuteRegisteredOperation` of lifecycle `OWNED_JOB` | `SlurmJobStepRunner` or `LocalJobStepRunner` | 5b |
| Operation | `ExecuteRegisteredOperation` of lifecycle `QUERY` or `IDEMPOTENT_WRITE` | `OperationStepRunner` | in use (5c) |
| Evaluation | `SubmitMeasurement` | `SlurmJobStepRunner` or `LocalJobStepRunner` | 5b |
| Adoption | `AdoptRevision`, `VerifyAdoption` | `AdoptionStepRunner` | in use (5c) |
| Workspace | `EnsureWorkspace`, `RestoreRevision`, `DiscardWorkspace`, `CloseAttemptScope` | `ExecutorStepRunner` | in use (5d) |
| Workspace | `SnapshotAndRetain`, `RetainRevision` | `ExecutorStepRunner`; they need their scope's writers ended first | in use (5d) |
| Session | `EnsureSession`, `CancelTurn`, `CloseSession` | `ExecutorStepRunner` | in use (5d) |
| Session | `SnapshotAndRetainRun` | `ExecutorStepRunner`; it needs the run's writers ended first | in use (5d) |
| Evaluation | `ObserveOwnedJob`, `CollectEvidence`, `CancelOwnedJob` | `ExecutorStepRunner` | in use (5d) |
| Reconciliation | `InspectRequest`, `CancelOwnedResource`, `BlockRequest` | none: deleted | deleted at the cutover |
| Session | `InspectTurn` | none: deleted | deleted at the cutover |
| Evaluation | `InspectOwnedJob` | none: deleted | deleted at the cutover |

Two things follow.

- **13 kinds had no owner:** `EnsureWorkspace`, `RestoreRevision`, `DiscardWorkspace`,
  `CloseAttemptScope`, `SnapshotAndRetain`, `RetainRevision`, `EnsureSession`, `CancelTurn`,
  `CloseSession`, `SnapshotAndRetainRun`, `ObserveOwnedJob`, `CollectEvidence` and `CancelOwnedJob`.
  `ExecutorStepRunner` (item 5d) runs them on their role's executor (`RequestExecutors`). It does not
  answer `Cleared` for everything: each kind declares in `DECLARED_REPEATS` why a second run reaches
  the same state, in one of four classes taken from the table above (`ReadOnly`, `FixedEnd` for
  `EnsureWorkspace` and `RestoreRevision`, `ReleaseOrBind` for the releases, binds and cancels, and
  `RetainByContent` for the snapshot and retain kinds). The constructor refuses a kind with no class and
  a kind another runner owns. `WorkspaceStepRunner` is a decorator, not an owner: it records and restores
  a workspace around an inner runner that runs the step.
- **The snapshot kinds depend on ordering, not on a reap.** The table above says `clear` of
  `SnapshotAndRetain`, `SnapshotAndRetainRun` and `RetainRevision` reaps the scope's tagged agent
  processes. A process carries the tag of its own step, so a snapshot step's `clear(step, tag)` cannot find
  them. What holds is that `decide` runs the snapshot only after every writer of the scope has a result,
  and a result is only recorded after its runner's `clear` or its own cancel path ended the processes. That
  ordering is `decide`'s rule from item 4a, and no test here proves it for the snapshot kinds.

### Marks on local stage work

A local evaluation runs its stages in a throwaway candidate session opened under `<run id>:<evaluation handle>`. Every stage command carries that mark as `VIBESYS_STEP_TAG` in its environment, and its process group leader carries it as an argument; a Docker candidate container also carries it as the `vibesys.step-tag` label. The run id is part of the mark because two runs on one host can use the same request id. What `clear` can find depends on what the host shows:

| Host | Shown | Ends | Guarantee against pid reuse |
|---|---|---|---|
| Linux | the environment of every process (`/proc`) | each marked process, including a `setsid` descendant | a pidfd is opened before the process's start time is checked, so a reused pid is never signalled |
| macOS | only arguments, so only the group leader | the leader's whole group | none atomic: the pid and its start second are checked, then the group is signalled; a wrong kill needs the pid space to wrap within that second and a new group leader to take the pid |
| Docker | the container label, read from the daemon | the container, listed again afterwards to confirm | the container id |

Not found on Linux: a process of another user (it cannot be this host's launcher's work), and a process of the same user that cleared its dumpable flag, such as an `ssh-agent` or a browser's sandbox, because `/proc/<pid>/environ` is then unreadable to its own user. Raising for them would make every clear fail on a desktop that runs one, and waiting would never end it, which is not the outage `ClearUnavailableError` is for. Any other failure to read the table (no file descriptors, no memory, an I/O error) is `ClearUnavailableError`, and a process still marked after the bounded wait that follows its SIGKILL is a survivor, as on macOS.

Not found on macOS: the members of a group whose leader died first, and a `setsid` child, because nothing visible carries the mark any more. `clear` then reports a clean host although they run. The leader is a shell that waits for its command and never replaces itself, so it dies before its group only by an outside `SIGKILL`. A durable record of the group, kept through `Project`, would close this; it is not built. Partial output is never reset: each open of a stage stream is a new stream with a new id, so a re-run starts empty and the dead attempt's bytes stay as its history.

### What fits none

- **Effects an agent performs itself** outside the workspace and the tool servers, such as a network call
  or a push from a shell tool. A re-run repeats them and no key can guard them. Agent confinement
  ([sandboxing.md](sandboxing.md)) bounds what a turn can reach; that bound is the only protection.
- **Descendants that escape the process group and the marker** (a `setsid` child). `clear` cannot prove
  none remain. A Docker run environment removes the container, which ends them. The Host run environment
  has no such boundary, so its `clear` can only kill a recorded process group, and a ghost write is caught
  by `AccessSettlement.settle` before a result is accepted. This is a named residual, not a proof. The
  marker sweep this page first described is not available there: the Host environment serves macOS, where
  `ps eww` does not show another process's environment (measured on Darwin 27.2: the marker of a child
  process owned by the same user is absent from its `ps eww` line), so no process can be found by the
  marker the way a Linux `/proc/<pid>/environ` scan finds it inside a container. The Host environment
  launches an agent the way a stage command is launched, led by a shell that carries the mark as an argument,
  and finds it through the process table of item 5d, so a Metal run on macOS starts and clears (see the
  per-environment risk below).
- **A cancelled adoption.** `Workspaces.adopt` publishes in a worker thread, and cancelling the step ends
  the wait for it, not the thread. The commit a straggler writes is the commit the next attempt writes
  (`adopt` is idempotent), so `AdoptionStepRunner.clear` removes nothing and only proves the workspace
  answers.
- **Usage of a killed attempt.** The provider may not report it. It is recorded as unknown, never zero.
- **A backend that offers neither a client-chosen id nor a list by label.** No `clear` can exist for it,
  so it cannot pass the contract suite and cannot be added as a step runner.

A step kind that is not safe to run twice is enforced by type in two places. `StepRunner.run` takes the
tag as a required argument, so a runner cannot start work with no key. A registered operation declares
what a rerun may repeat as a `RerunPolicy` when its owner enters the catalog (`OperationEntry.owned`
takes it as a required argument): `ReadOnly`, `KeyedWrite` (names the key its owner reads back through
`inspect`), `ClearedWork` (a cleaner that kills the process group and resets the workspace) or
`OtherRunner` (a job or a turn, whose own runner declares what a rerun repeats). The entry refuses,
naming the operation kind, a policy its lifecycle does not allow (`QUERY` takes `ReadOnly` or
`ClearedWork`, `IDEMPOTENT_WRITE` takes `KeyedWrite` or `ClearedWork`, `OWNED_JOB` and `SESSION_TURN`
take `OtherRunner`), and `OperationStepRunner` refuses a step of an operation another runner owns. The
table above classes `capture_command` and `validate_local` as `ClearedWork` and `export_patch` as read
only, which is how their lifecycles are declared in code. The production roles carry their policy in one
table in `_operation_wiring.py`. The two command roles take the cleaner the run supplies; a run that
supplies none gets `UnwiredWorkCleaner`, whose `clear` raises `ClearUnavailableError`, so the step loop
waits on that runner visibly instead of repeating a command whose process nobody ended. The cutover
(PR 6a) wires a real cleaner.

Doc fact corrected against code (item 5c): the first version of this page put `export_patch` among
the keyed writes. Its owner exports a patch and writes nothing, and it is declared `QUERY`.

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
| `strategy` and `event_cursor` | envelope | Logged by the search itself, one `StrategyAdvanced` record per advance ([The search in the log](#the-search-in-the-log)); a resume on the same code replays the journal it holds |
| `fence` | envelope | Replaced by the run lock |
| `pending_inputs`, `pending_decisions`, `wakes`, `pending_publications`, `delivery_cursor` | runtime record | Derived, except the owner events: the ledger derives the owner event of a report (`_request_forward.py`) but not every fact an executor tells an owner (a turn's reply, a refused mid-turn steer, a job's failure class, an adoption's publication), so those travel in the `StepFinished` record ([Owner events](#owner-events)). Decisions and wakes are the strategy's output; publications are the fold's output. The delivery cursor is the one fact that is not derived: after a restart every publication since the last recorded acknowledgement is delivered again |

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
   the divergence. Resume across a code change that reorders a flow is out of scope here. A resume on
   changed code restarts the flows from their last checkpoint and lets go of work the new code does not
   ask for in that restart step: the dead flows' request is cleared, its workspace is settled and holds
   no slot, and a flow that asks for the same work later gets new work.
3. **Complete inputs.** Every external input is a record.

New records (one proto envelope, `vibesys/store/v1/core_input.proto`, validated with protovalidate):

| Record | Carries | Why |
|---|---|---|
| `RunStarted` | the `CoreState` the run starts from, deadline included | seed of the fold; the first core record of a run, so every process folds from the same state |
| `StrategyAdvanced` | the proposals core took, the steps the search's journal gained or its whole state, the wake-ups it asked for and delivered, the decisions core could not take yet | one advance of the search, in one record, so what it proposed and what it kept cannot disagree after a crash |
| `CoreInput` | a decision or proposal, an owner event, an operator control, an epoch resume, a clock advance that changed state | the external inputs of `step` |
| `StepStarted` | tag, attempt, request, deadline | durable intent before any I/O; makes a re-run visible |
| `StepFinished` | tag, attempt, `Finished` (the report and the owner events), `Failed` or `Cancelled` with a typed reason | the result; one per step, and one atomic record, so a crash loses no owner event |
| `AttemptInterrupted` | tag, attempt, reason (`PAUSE`, `SHUTDOWN`, `RAISED`) | an attempt that ended without ending the step; a `RAISED` one (the runner raised or gave nothing back) is charged to the retry bound, the other two are not. The loop writes `PAUSE` and `RAISED`; an orderly stop of the process (`SHUTDOWN`) is not built, and a cancelled loop is a crash |
| `RunEnded` | the run's result | the end of the run, published once nothing is open; a run has ended when this is in the log, not when the kernel reaches its terminal status |
| `CoreSnapshot` | an old `RunEnvelope`: its core state, the owner events it had not applied, the search it left (state, wake-ups, held decisions) | the seed of a migrated or imported run only; never written for a new run |

The bodies of decisions, requests and observations are the canonical encoding of today's validated
`CoreEvent` and `Request` values in a bytes field with a schema version, the same interim form
`reply_json` and `LegacyEvent` already use. Core has proto types for ids, scopes, limits, controls and a
few leaf values (`proto/vibesys/core/v1`). Generating the 24 request kinds and the event kinds (about 200
`kind` literals in `vs_core/types`) is the phase 3 work of [run-transport.md](run-transport.md), not a
prerequisite of this design. A migration from
an existing run's `store.json` writes one `CoreSnapshot` record holding the old envelope as the seed of
the fold, with its recovery fields dropped and its open requests becoming open steps. The migration code
is deleted seven days after it ships.

As built (PR 6a-3m), the migration is `vibesys.run.store_migration.migrate_saved_run`, called by a
resuming process after it opened its epoch and before it opens the step log. It appends nothing if the
log already holds a start of the run (`vs_core.api.recovery.starts_run`), returns `NOTHING_SAVED` if the
store holds no run, and otherwise reads the store as a resume does (`resolve_core_resume`, so a store of
another run or strategy is refused with its diagnostic) and appends one `CoreSnapshot`. The record carries
the core state and the search's state compressed, the owner events the saved run had not applied (the
fold applies them in order after the seed, as `CoreInput` records would be) and the search's wake-ups and
held decisions (`restore` reads them as a checkpoint). Core takes and returns plain values: it encodes a
`SavedRun` as canonical JSON bytes (`encode_saved`) and folds one (`apply_saved`, `restore(saved=)`). The
compression and the `CoreSnapshot` message are `vs_runtime`'s (`_saved_run_record.py`), and so is every
read of a log that may hold a snapshot (`fold_log`, `restore_log`): core refuses a snapshot record it is
handed directly. One record, so a crash leaves the log with all of
it or none, and the migration is idempotent at every append. Dropped: the fence, the event cursor and the
publications to the display journal the saved run had not delivered. A saved run that does not fit one
record after compression (a record is at most 1 MiB) is refused by name with `SnapshotTooLargeError`; one
saved core state measured 708 KB of JSON and 99 KB compressed. The store is left as it was.

Deleting the migration seven days after the cutover ships is not enforced: a check on the date would
depend on real time. The pull request that ships the cutover names the date. What stays for good is the
log reader's reading of `CoreSnapshot` (`vs_runtime`), which every migrated log holds. Until the module is deleted, the cutover's
deletion of the old store (PR 6b) must keep what the migration reads: `StateStore.load`,
`RuntimeRecord`, `resolve_core_resume` and the envelope migrations of `vs_core`.

### The search in the log

The strategy needs more than core's state: its flows are suspended mid-search, and a resumed process has
to hold them where they were. Appending the whole state after each advance grows with the square of the
advances (measured: 33 MB at 130 advances of a dynamic search, 150 MB at 290, about 2 GB at 1,000, a record
of up to 4 MB each), so the log keeps the journal's steps instead, 2 to 2.6 KB per advance.

- One advance is one `StrategyAdvanced` record: the proposals core took, the journal steps the search
  gained, and the wake-ups it asked for and was given. The fold applies the proposals; nothing of the search
  runs at fold time. The events core published since the last advance record are kept by `fold_run`, so a
  process that takes a log over after a crash lets the search hear them first.
- The whole state is logged when a run restarts, each time the number of advances reaches
  `CHECKPOINT_GROWTH` (2) times that of the last whole state, and when the run parks or ends. The log then
  holds about twice its last state in checkpoints, however long the run, and grows with the steps.
- A resume on the same code replays the journal through the flow driver's own digest-guarded replay
  (`FlowDriver.resume`); there is no second replay. A digest that differs means the code changed.
- A resume on changed code restarts the flows from the checkpoint the log holds. After a clean pause or a
  finish that is the latest state, as it is today. After a crash together with a code change it is the last
  whole state, at most `1 - 1 / CHECKPOINT_GROWTH` of the journal old; the decision numbers are kept
  current in the steps, so the restarted flows never reuse the id of a decision the dead ones issued.
  Tightening that age is a change to `CHECKPOINT_GROWTH` alone, and nothing else depends on its value.
- A decision the search held while the run was paused is offered to core only if the flows still stand
  behind it after the search has heard what arrived: a restart gives the flows a new incarnation that asks
  again for what it needs.

A run is never left silent. `CoreInputs` brings in what is due (the operator's controls, the deadline stop,
the clock when it changes core's state, the search's wake-ups), and a run that waits on nothing, with no
step in flight, no timer, no wake-up and no operator input that could move it, raises
`StepLoopStalledError` naming the open records, the waits nothing will end and the work the search has
left.

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

A fold over the whole log grows with run length. The cache bounds it to the records since the last
keyframe, and a property test fixes that a cached start and a full fold give equal states. Measured on a
laptop (a `StepStarted` and `AttemptInterrupted` pair: 17 microseconds per record up to 4,000 records;
a `CoreInput` that prepares a request: 0.3 to 0.6 milliseconds, the cost of `step` itself, which grows
with the ledger): a log of 4,000 attempt records folds in 70 milliseconds, and `decide` takes 0.04
milliseconds. A start that folds the whole log of a run with thousands of inputs is a second or more,
which is why the cache exists; the keyframe interval is the cutover's to choose.

As built (PR 6a-3m):

- `vs_runlog.FoldCache` is the interface (`load`, `store`). `FileFoldCache` writes `state.pb`, a
  `StateCache` message, with a temporary file, an fsync and a rename. `MemoryFoldCache` is the Fake, as
  strict as the file, and both pass `FoldCacheContract`. The cache holds the bytes of a fold (the
  `Folded` of `vs_core`, encoded by `Folded.encode`: core's state and the events the search has yet to
  hear), the number of records it covers and the SHA-256 of those records.
- A start (`start_fold`) uses the cache only if its digest is that of the log's own first records.
  Otherwise it folds the whole log and says why in a typed `CacheMiss`: `ABSENT`, `UNREADABLE`,
  `AHEAD_OF_LOG`, `DIGEST_MISMATCH` or `STATE_REFUSED` (the fold in the cache cannot be read by this
  code). `RunLogStepLog.cache_start` reports which. The cost that remains is hashing the log's records
  (about 4 microseconds each) and the search's restore, which still reads the log's advance records.
- `RunLogStepLog` writes the cache when the run comes to rest (parked or ended) and every `CACHE_EVERY`
  (1,024) core records its process appended. The keeper folds the log itself from where its cache stopped
  and does not copy the step log's state: the step log folds only the records it appends, and the display
  projection's records, which a fold reads for their epoch and clock, are in the log between them. A cache
  written from the fold a later start computes cannot differ from it. A failed write leaves the cache
  before it, which is only older.
- Not covered: a process that changes the fold's code between the write and the read. The cache is read
  as the code that reads it decodes it, and a state it cannot decode is `STATE_REFUSED`.

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
    clear_first: bool       # work of the step may exist: its runner's clear comes before the attempt

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
class Tick:
    at: float               # record the run clock: time changes the log only through a record

@dataclass(frozen=True)
class End:
    result: RunResultProposal

type Action = Run | Cancel | Settle | Wait | Tick | End

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
when the executor reported more (a turn's reply, evidence, a revision), the whole `RequestObserved`, and
the owner events the executor returned beside it. `FailureReason` gains `ATTEMPTS_EXHAUSTED`.

#### Owner events

An executor answers a request with a report and, for the requests whose owner needs more than the
report says, owner events. The live shell applies the report first and then each owner event in order
(`ExecutionResult` in `vs_runtime/_core_requests.py`). The fold does the same from one record:
`Finished.observed` is the report, `Finished.owner_events` the events (`vs_core.types.steps.OwnerEvent`:
the attempts, sessions, evaluation and settlement events), and the fold steps the report and then each
event. The ledger derives an owner event of its own from a report (`owner_signals`), so most events
are redundant with the report, but these facts exist only as the executor's event:

- a plain turn's reply (`TurnObserved`; the ledger withholds its own while a reply is owed);
- a mid-turn steer the provider refused (`InputMidTurnReleased`);
- the executor's classification of a failed job (`JobObserved.failure`) and its diagnostic;
- an adoption's publication and checkout (`AdoptionObserved`).

The fold refuses a record whose observation or owner event names another request, scope or admission,
as the live shell does. A request that core settled itself (`Failed`, `Cancelled`) has no owner event:
the ledger derives the owner's from the conclusive observation core writes.

One fact the order would lose is carried by the report instead. An adoption's publication and checkout
are fields of `RequestObserved`, and `owner_signals` copies them into the `AdoptionObserved` it derives,
so the derived event is as complete as the executor's own and a succeeded adoption is not followed by a
`VerifyAdoption`. `AdoptionStepRunner` and the workspace executor both fill them.

`decide(fold(log), now)` is the issue's `decide(log) -> actions`. It is split in two so a cached state can
stand in for the fold. The action type has no `Blocked` or `Unknown` variant, and no variant names a step
kind. The rule is:

- an open step with no running attempt, whose dependencies hold and whose run may start steps, is `Run`,
  with `attempt = 1 + the latest attempt that began`; its deadline is the step's own limit, counted from
  its first start, and a later attempt keeps it;
- a running attempt past its deadline is `Cancel(DEADLINE)`; an open step with no running attempt past its
  deadline is `Settle(Cancelled(DEADLINE))`, because there is nothing to cancel;
- an open step whose attempts are spent is `Settle(Failed(ATTEMPTS_EXHAUSTED))`: attempts that began, less
  the ones a `PAUSE` or `SHUTDOWN` interruption ended, exceed the step's retry limit (`retry_limit` of
  the request, else `Limits.request_retries.limit`), so an attempt that raised or whose process died is
  charged and a step that always does ends as a typed failure;
- an open step marked `on_interrupt = CANCEL` whose attempt ended without a result, by an interruption
  record or by its process, is `Settle(Cancelled(INTERRUPTED))`;
- an open step whose dependency can never succeed is `Settle(Cancelled(WITHDRAWN))`; one whose dependency is
  still pending waits;
- a paused run (`PAUSED`, `DRAINING`) starts no step except, while `DRAINING`, work the drain admits
  (`_run_park.dispatch_allowed`); a running step is `Cancel(PAUSE_NOW)` when Pause now withdrew it and
  `Cancel(DRAIN_EXPIRED)` once the pause's drain deadline (`RunPark.deadline_at`) has passed
  (`_run_park.pause_cancels`);
- a parked run is at rest: it starts and settles nothing, whatever its steps hold or however late the clock is, and
  an attempt it still has running gets `Cancel(PAUSE_NOW)`; with none, `decide` returns only `Wait`;
- a run whose committed finish is a cancel, or that has ended, gets `Cancel(RUN_ENDING)` for each running
  step and `Settle(Cancelled(RUN_ENDING))` for each other open step, and starts none;
- a step that snapshots its scope's files waits while any step that writes them is open, and a step that
  writes them waits while a snapshot of its scope runs ([Scope order](#scope-order));
- a run with a committed finish, no open step and no `RunEnded` record is `End`; once the record is in
  the log the run has ended and `decide` returns only `Wait`.

A step runs now when its latest attempt has no record ending it. The first record of a later epoch (the
epoch header, `EpochStarted`, which the writer appends before anything else) ends every attempt that
began before it: the run has one writer, so the earlier process is dead. The fold does this, not
`decide`, so a state cached before the restart and a full fold agree, and `decide` issues `Run` for those
steps as soon as the header is in the log. The ended attempts are charged, as a raised one is. Cleanup steps that core issues while a cancelling finish is committed are cancelled
like any other; whether cleanup should instead outlive the finish is left to the cutover.

#### Rest, drain, end and resume

The facts of the rule the loop depends on, each held by `libs/vs-core/tests/test_recovery_gaps.py` and,
over generated trajectories, by `test_recovery_progress.py` over every kind of step, a lagging loop and the seeds a restart finds, and
named one by one in `test_recovery_findings.py`: the fold accepts the record of every action `decide`
returns, applying an action changes the state, no `Wait` is already due, a control the log holds does what
it says or was refused to its sender, and a run left to cooperative runners comes to rest and records each
end.

- **Parked is at rest, and a recorded result is never dropped.** A parked epoch ignores what is offered
  to it (`parked_epoch_drops`). That is a filter on live input. A fact the log holds is history, and the
  fold applies the report and the owner events of a `StepFinished` through `step_recorded`, which does not
  drop. `decide` settles nothing in a parked run, so it never asks for a result the kernel would ignore;
  it still cancels an attempt the park found running, because the kernel can park at a Pause now before
  the loop has stopped what runs, and a flight nothing cancels would hold the process up.
- **A draining pause parks when nothing it waits for is left.** An attempt that ends without a result (an
  interruption, a raise, a crash into a new epoch) returns its step to the state of a step no one has
  started, as a drain that interrupts a request does (`resent`), and the fold then advances the drain
  (`settle_park`): the run is `PARKED` once nothing it waits for is left. What a drain waits for is the
  question `decide` asks of a prepared step, `_run_park.starts` (dependencies met, the status admits it,
  no step of its scope must end first), so a step the rule holds back can never keep a pause from
  parking. `RunPark.deadline_at` comes
  from the pause's `drain_seconds`; `decide` cancels with `DRAIN_EXPIRED` once it has passed. The open
  step stays open and runs again, uncharged, in the epoch that resumes the run.
- **The end of a run is a record.** A stop with nothing open takes the kernel to its terminal status at
  once, before anyone has published the result. `RunEnded` is that publication: the loop appends it on
  `End`, which `decide` returns only once the kernel is terminal. `at_rest` reports `ENDED` only when
  the record is in the log, and it stands only while the run still holds that result: a resume clears
  it (`end_lifted`), so a run stopped again with the same result records a second end. The kernel
  notices that the last step has its result at its next clock input, which a live shell sends on a
  timer and a fold has not, so the fold applies that wake after each record (`settle_end`). The fold
  refuses a `RunEnded` while the run is not over or with a result the run does not hold.
- **A parked run resumes only in a later epoch.** A pause exits the process, so the park lasts for the
  rest of the epoch it was made in, and the kernel refuses a resume there ("pause is terminal for this
  epoch"). Resume is a relaunch: the new process opens the next epoch (`EpochStarted`), which moves the
  kernel's host epoch with the log's and leaves the parked run parked, and the resume control is then an
  ordinary input that the fold takes.
- **The pause epoch and the drain's authority are different things.** `RunPark.epoch` is the epoch the
  operator paused in and never moves. A drain keeps its authority over the work in flight in whatever epoch
  it is (`drain_in_force`: still draining, or parked in this epoch), so a Pause now still cancels after a
  kill. A drain a kill cut short therefore parks in the next epoch with a park that is older than that
  epoch, and the Resume that follows is admitted there at once instead of being refused.
- **A drain that ran out its deadline parks in the log.** Time changes the log only through a record, and
  the deadline passing writes none: a run that waited only on attempts the loop cancels would stay
  draining in the log until some unrelated step happened to end, and whether a held attempt was charged
  would depend on that. `decide` asks for the clock instead (`Tick(at)`), the loop appends it as
  `ClockAdvanced`, and the fold parks the run. The loop never reports `PARKED` while the log says
  draining: a pause with no deadline waits for its attempts, and a Pause now or a passed deadline parks.
- **A run may park with an attempt the loop still runs, and the epoch end ends it.** A drain that cancels
  (a Pause now, or a pause past its deadline) does not wait for the attempt that runs a request
  (`_holds_park`): `decide` cancels it in the parked run, and a call that ignores the cancel dies with the
  process. The first record of the next epoch ends that attempt as interrupted, not charged like a crash
  (`epoch_opened`), because the pause ended it; one a draining run held when it died is charged. A
  parked run therefore never holds an attempt that nothing will end, and the only attempts it holds are
  those of a call the loop showed held (`StallBoard`), which the process exit ends.
- **Core says whether work may exist.** `Run.clear_first` is true whenever the log or its seed shows the
  step was sent: an attempt began, or an earlier version dispatched it. The loop clears exactly then, and
  never from the attempt number. A seed step in flight has no attempt in this log, so the fold gives it
  one, the first, ended and uncharged: the step runs next as attempt 2, a re-run that clears what the
  first left. A seed step that was never sent has nothing to clear and starts as attempt 1.
- **A settlement reaches a flight as a cancel does.** `decide` gives a step with no running attempt a
  `Settle`, also while the loop has a flight for it waiting on a `clear` before it starts. The flight
  takes the reason as it takes a `Cancel` (the first reason is the signal's), so a stop ends a run whose
  next attempt waits on an unreachable backend: an ending is one fact whichever action carried it.
- **An adopted result is the step's result.** When `clear` adopts work that ended while no host ran, or the
  loop's attach to adopted work returns its own result, that result is recorded, not the settlement core
  decided in its absence.
- **A step's limit is its own.** A drain caps the request it cannot interrupt at its deadline plus the
  cleanup bound, and the cap belongs to the attempt in flight. `StepAttempts.deadline_at` keeps the limit
  the first attempt started with, and whoever returns the step to unstarted (the fold, on the record of
  the attempt's end, or the drain, which lets a job go at once) gives it back (`unstarted`), so a step
  that was paused and resumed has the limit it would have had.
- **A pause leaves the jobs of a loop-hosted run alone.** The request that watches a job goes back to
  unstarted, the job runs on, and the next epoch runs the request again, where its runner finds the job
  with `clear` and attaches to it. The kernel issues no cancel of a job and holds no park for it. A run
  hosted by the executors of the layer being replaced (`steps.epoch == 0`) keeps the park's cancellation
  of its jobs until the cutover removes them.
- **A stop ends a run in a pause.** A draining or a parked run holds its search still, but the operator's
  stop is not the search's: it takes the `Finish` through `takes_command(stopping=True)`, closes the park
  and ends the run, instead of a rejected decision no one hears of.
- **A seed is settled once, and the log numbers its own epochs.** The first epoch the fold opens starts
  a numbering of its own, so a seed's host epoch and the park it holds are older than any record that
  follows (otherwise the first Resumes are refused until the log catches up with the seed). What an
  earlier version left in flight is settled the way this version reads it: an inspection, a cancel or a
  block still open lapses (nothing here sends them), and a step in flight with no attempt in the log
  goes back to unstarted. The kernel registers such commands again when a limit passes under a clock
  input; the fold lapses them after each input.
- **A step core ended itself is concluded the way any step ends.** A clock input past a step's limit ends
  it in the kernel while the loop's attempt may still run. `decide` cancels the flight (or settles a step
  with none) until the runner's end of the attempt concludes it, which is what releases a job or a turn it
  owns, so a stopped run can close; the fold keeps a late report of such an attempt as history, not as a
  result.
- **The log is read as strictly as it is written.** A `StepFinished` names the latest attempt the step
  began, a `RunEnded` a run that is over, and an interruption or cancel reason the version knows; the
  fold refuses the rest as a `RecordRefusedError`, never a `KeyError`. It is a `ValueError` that carries a
  `RecordRefusal`: the closed `RefusalReason` (`NO_SEED`, `STEP_NOT_OPEN`, `START_NOT_DUE`, `ATTEMPT_MISMATCH`,
  `RUN_NOT_OVER`, `RESULT_MISMATCH`, `UNREADABLE`, `SEED_REPEATED`), the step it names, and the message. No
  fold module raises a bare `ValueError`.

- **The log holds only starts the rule would run, checked and stamped at one instant.** A flight that
  waited for its `clear` or for the writer while the run paused, parked or ran out the step's time would
  record a start into a run that cannot take it, leaving an attempt that nothing cancels. The fold refuses
  a `StepStarted` that `decide` would not `Run`. The loop does not ask first with its own clock: that clock
  reading and the log's stamp are two instants, and a pause or a limit can fall between them.
  `step_started(run)` reads the run clock once, asks `start_due(state, tag, now)` (the `Run` the rule gives
  at that instant, or a `RecordRefusal`), and stamps the record with that same `now`. It returns the attempt as recorded,
  which the loop runs: an unstarted step's limit counts from the stamp, so a start that waited is not born
  expired. When the rule would not `Run` the step then, it appends nothing and returns the `RecordRefusal`
  (`STEP_NOT_OPEN` or `START_NOT_DUE`, the two a race explains; an attempt out of order is no race and
  raises). A flight that is refused stands down: its `clear` did no harm and the step stays open.

`open_step(state, tag)` is the step as the loop runs it while it has no result: its request, its limit
and the attempts that began. The loop reads it from core instead of rebuilding it from the ledger.

#### Scope order

A snapshot of a half-written tree is the one effect `clear` cannot undo, and `clear` cannot prevent
it either: a process carries the tag of its own step, so the snapshot's `clear` has no tag to reap by.
The rule orders the steps of one scope instead (`vs_core/_scope_order.py`). `request_effect` classifies
every request kind as `WRITES`, `SNAPSHOTS`, `CLOSES` or `NEITHER`, beside `request_lifecycle`, so the
rule names no kind:

- `SNAPSHOTS`: `SnapshotAndRetain`, `SnapshotAndRetainRun`, `RetainRevision`, and a registered
  operation whose descriptor has a revision authority of snapshot or retain;
- `WRITES`: a turn whose session is not read-only, `EnsureWorkspace`, `RestoreRevision`, and a
  registered operation that is not a query, or that has no descriptor, because a descriptor does not
  yet declare whether it touches the tree;
- `CLOSES`: `CloseAttemptScope`;
- `NEITHER`: every other kind.

A `SNAPSHOTS` step runs only while no `WRITES` step of its scope is open, and a `WRITES` step starts
only while no `SNAPSHOTS` step of its scope has a running attempt. Open means no result: a writer whose
attempt a pause interrupted, a runner raised or a crash ended still holds the scope back, because its
files are half written and its next attempt resets them before it runs. A writer that depends on the
snapshot, directly or through other steps, cannot end before it and does not hold it back. A snapshot
blocked this way still ends: every writer has a limit, and the snapshot's own limit settles it. A
`CLOSES` step runs only when every other step of its scope has a result, because a step that began
before the close and reports its job after it would leave that job running with no one to cancel it;
a step that waits for the close itself does not hold it back. The close waits for a running step to end
by itself or at its limit: the rule does not withdraw the work of a closing scope, because nothing kind-free
yet tells work whose result is no longer wanted from the cleanup the close depends on. What
is missing for a precise rule is a declaration on `OperationDescriptor` of whether an operation
writes the tree; until then every operation that is not a query counts as a writer, which only delays
a snapshot. `libs/vs-core/tests/test_recovery_scope_order.py` holds the rule over generated states.

`decide` is total: it never raises on a validated log, and a log that fails validation is a read error at
the boundary (a corrupt record stops the reader, as `vs_runlog` does today), never an action.

Two more facts of the rule belong to core, so the loop does not restate them. `at_rest(state)` is `ENDED`
when the run is `TERMINAL` and `PARKED` when it is `PARKED`, else `None`. `interruption_of(reason)` says
whether cancelling a running attempt for `reason` is an interruption of the attempt (`PAUSE_NOW` and
`DRAIN_EXPIRED`, recorded as `AttemptInterrupted(PAUSE)`, the step staying open) or the step's result
(every other reason).

### vs-runtime: runner loop and StepRunner

`vs_runtime.api` owns the Protocol, because `vs_runtime` already owns the seam where core requests meet
I/O libraries (`_core_requests.py:ExecutorRole`, `RequestExecutors`) and the libraries below it cannot
import it. Each runner is a `vs_runtime` implementation that adapts one library's own typed interface
(`vs_agent`, `vs_slurm`, `vs_project`). The library supplies the mechanism (kill by tag, adopt by ledger
key); the issue's wording "one `StepRunner` each in `vs-agent`, `vs-slurm`" would need an upward edge.
Implementations are named `<Variant><Role>`: `FakeStepRunner`, `AgentTurnStepRunner`,
`WorkspaceStepRunner`, `SlurmJobStepRunner`, `LocalJobStepRunner`, `OperationStepRunner`,
`AdoptionStepRunner` and `ExecutorStepRunner`.

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

Contract, which every implementation passes in `vs_runtime.api.testing.step_runner_contract`:

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
        Run:    if clear_first: await runner.clear(step, tag)
                append StepStarted(tag, attempt)      # durable before I/O
                start runner.run(...) as a task
        Cancel: set the signal; on return append StepFinished or AttemptInterrupted
        Settle: await runner.clear(step, tag) if an attempt was interrupted; append StepFinished
                (step: from the fold, for a step with no attempt in flight)
        Wait:   await the first of: a task ending, a new input (a pause is one), the timer
        Tick:   append the run clock as an input (a drain's deadline passed)
        End:    append RunEnded; release the lock
    state = fold(state, the records just appended)
```

The loop owns timers, tasks, cancellation and recording. `decide` owns every decision, including that a
deadline passed. No `if isinstance(step, ...)` appears in either.

In code the loop decides with core's own functions, `decide` and `at_rest`, and sees the log through one
interface it owns. `StepLog` is the log as the loop uses it: one atomic durable append per method, a
`state` that is the fold of everything appended (by the loop or by an input from elsewhere), and
`open_step(tag)`, the step with no result as the fold has it (its request, its limit and its latest
attempt), which is where `clear` gets the step for a tag with no attempt in flight. `step_started(run)` is
the one append that decides: it returns the attempt as recorded or a `RecordRefusal`. `clock_advanced(at)`
records the run clock when core asks (`Tick`). `StepLogContract`
holds a log to what the loop relies on, and a restarted log over the records of a dead process is how the
tests kill the host. The loop's clock is the run clock, the one `decide` reads and the records carry.
`at_rest(state)` says the run ended (the loop returns at once, so a restart never records the end twice)
or parked (it returns once nothing is in flight). Each running attempt is a flight, one task that owns the
attempt from `clear` to the record of how it ended, so a `clear` that waits on an unreachable backend never
blocks the loop or an ending. How an attempt ends depends on why it was cancelled:

| Cancel reason | Recorded | `clear` unreachable |
|---|---|---|
| `PAUSE_NOW`, `DRAIN_EXPIRED` | `AttemptInterrupted(PAUSE)` | give up; the next epoch's `clear` handles the leftovers |
| `RUN_ENDING` | `StepFinished(Cancelled)` | give up; the leftovers are a named residual |
| `DEADLINE`, `WITHDRAWN`, `INTERRUPTED` | `StepFinished(Cancelled)` | keep retrying with backoff |

One flight holds a tag at a time, and holds it until every `run` and `clear` it started has returned. A cancel
cannot stop a thread, so the awaitable finishing is the only proof that its work ended: a call that ignores
its signal gets its task cancelled as a nudge and is then waited for, never given up on. Nothing starts, clears
or records on that tag meanwhile, and the tag is on the stall board (`StallBoard`, handed to `run_step_loop`)
with its kind (`run-held`, `clear-held`, `clear-unavailable`, `clear-failed`, `still-running`), how long, and
the error that kept it there. A result is recorded only over proof: a `Cleared`, an `Adopted` whose work
ended, or a returned result. A `run` that raised or was cancelled, an attach to adopted work that did not end
it (a job still completing after its `scancel`), and any `clear` that raised have proved nothing, so the loop
asks `clear` again with backoff; what a `clear` raised stays on the board, so a programmer error is visible
in the stall and not swallowed. Ending the run treats a held tag as an open flight: `End` waits for it, and the
stall shows. An operator who cannot wait kills the process; the next process's `clear` and `End` finish the
run, because a crash is always recoverable. The one residual is a `clear` that cannot return at all while the
run ends: the step is recorded cancelled without proof and listed in `StallBoard.unproven`. A runner whose
call blocks in a thread must shield the thread, so that its awaitable ends only when the thread has: a call
that returns on cancel while its thread goes on is invisible to the loop.

An attempt that returned a result on its own is recorded as that result even when a cancel was set, unless
the run is parked: core drops a result in a parked epoch, so the attempt is recorded as
`AttemptInterrupted(PAUSE)` and the step runs again after the resume. A parked run also takes no `Settle`: it
waits for the resume, so a step past its limit is not settled over and over. An
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
runs it again as the next attempt. An attempt a pause interrupted is neither a policy failure nor a retry debit,
as [run-transport.md](run-transport.md) already says; one whose runner raised is charged. The drain deadline is a field of the pause input,
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
| 4b. Loop and Protocol | `libs/vs-runtime/src/vs_runtime/_step_loop.py`, `_step_runner.py`, `api/step_runner.py`, `api/testing.py` (`FakeStepRunner`, `step_runner_contract`); `tach.toml`; `libs/vs-dst` hooks (#3475 item 2 owns the points) | nothing |
| 5a. Agent turn and workspace runners | `libs/vs-runtime/src/vs_runtime/_agent_turn_step_runner.py`, `_workspace_step_runner.py`; `libs/vs-agent/src/vs_agent/orphans.py` gains reap by step tag beside reap by run | nothing |
| 5b. Evaluation runners | `libs/vs-runtime/src/vs_runtime/_slurm_job_step_runner.py`, `_local_job_step_runner.py`; Slurm tier case | nothing |
| 5c. Operation, adoption and environment runners | `libs/vs-runtime/src/vs_runtime/_operation_step_runner.py`, `_adoption_step_runner.py`; per-environment `clear` in `_run_environment.py` (Docker, SkyPilot, Modal, Host) | nothing |
| 6a. Cutover | `src/vibesys/run/core_run.py`, `core_services.py`, `src/launch`; `vs_runlog` `state.pb` cache writer; the one-time `CoreSnapshot` migration; `libs/vs-dst` runs the production loop | none yet; the old path is unreachable |
| 6b. Deletion and guard | the files in the [inventory](#consumer-inventory); `tests/architecture/test_no_prove_or_block.py`; `tach.toml`; this page's status table, [run-transport.md](run-transport.md) commit text | about 5,700 production lines plus about 200 dormant keyed lines (`_replay_store.py` and `api/replay.py`), and their tests |

PR 6a and 6b land back to back, with no cleanup deferred, as #3408 required of its own production switch.

### What of keyed replay stays

- **Saved-result reuse stays as the ledger already does it.** A finished request is a `COMPLETED` record in
  the fold, and the shell dispatches only `PREPARED` records
  (`libs/vs-runtime/src/vs_runtime/_core_loop.py:CoreRuntime._authorized`). No keyed code is needed for it.
- **Kept, moved:** the log-append commit discipline of `RunLogReplayStore` (torn tail repaired, complete
  corruption fails, uncertain commit authorizes no dispatch), into `vs_runlog`.
- **Deleted:** the whole `vs_runtime` driver half (`_replay_runtime.py`, `_replay_run.py` and the fourteen
  modules around them, 4,323 lines), the pure `vs_core` half (`flows/_replay_*.py` and `api/replay.py`,
  about 4,300 lines), `src/vibesys/serial_replay.py`, the writer barrier in `_writer_reconciliation.py`,
  and the tests, benchmark scripts and test-only proto fixtures of all of them. The `ReplayUnknown`,
  retire and withdraw paths went with the runtime. The code is the path to reusing results across a code
  edit by semantic key, which this design does not need; it is recoverable from git history (#3378 and the
  parts before it).
- **Moved:** `ReplayRootReuse`, the one `vs_core` type live code still needed, now lives in
  `_writer_reconciliation.py` beside the registry that issues it.
- **Kept for 6b, listed:** `_writer_reconciliation.py` and `_evaluation_recovery.py` (imported by
  `_run_environment.py` and `_polling_evaluation.py`, which other recovery branches edit), the `replay*.proto`
  messages and their generated clients, and `_replay_store.py` with `api/replay.py`. Their protos are
  removed in 6b; `buf breaking` flags the removal, so 6b names an exception, valid because no run has
  written those messages. `vibesys.fact_import` still builds `AccountingEvidence` (from those protos) for
  retained-run import; no run path calls `accounting_evidence`, and the only consumer of its output was the
  deleted keyed ledger, so 6b decides whether it goes with them.

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
| Resume on edited code | neutral | Same rule as today (next round). The deleted keyed-replay code (see git history) is the path to more |
| Retained-run import | neutral | An imported run starts from a `CoreSnapshot` record, as a migrated one does |

## Open pull requests

#3458 changes `_recovery_owners.py:known_owner`, and #3451 changes
`libs/vs-runtime/src/vs_runtime/_replay_run.py:_LiveEpoch`, a file the keyed-replay deletion removes. Both are
members of the class this page removes. #3458 becomes unnecessary: with no recovery check there is no
`known_owner`, and a session that ended without acceptance has a recorded result like any other step.
#3451 becomes unnecessary: its only file is deleted, so it closes without merging. Neither should be
reworked to fit this design. Merging them first costs nothing
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
- **Registered `SESSION_TURN` operations carry the reply as a turn does.** The runner puts the reply in the
  report's `outcome_schema` and `outcome_json`, and leaves `operation_schema` unset: setting it needs the
  registry's codec context, which only the wiring has. No production operation has that lifecycle today, so
  the typed outcome of one is unproven until 6a wires `OperationTurns`.
- **Provider session id of a killed turn.** `forget_unfinished` does not touch the provider conversation. A
  provider that rewrites its session id mid-turn makes the retry's expected-checkpoint check fail loudly
  (`SessionResumeError`) instead of resuming; the fix belongs to that provider's contract case.
- **The executor kinds repeat by tag, without receipts.** The loop records an attempt before any of its
  I/O, so an attempt after the first proves an earlier one may have run. `ExecutorStepRunner` passes that
  as `ExecutionContext.repeat`, which replaces the `resumed` flag the begun marker of `run_once` used to
  give (`run_once` now hands its performers `repeat` from the context or the marker, so the production
  loop is unchanged). Every test of a repeat runs the real executors over an `UnreceiptedStore`, which
  keeps no execution record: each of the 13 kinds is killed at sampled durable writes (every write, for
  `EnsureSession`), cleared and run again, and so is a generated program of runs, kills, restarts and
  clears, with the effects and the result of an unkilled run. Two kinds were safe only through the
  marker:
  - `DiscardWorkspace` answered `children_complete=False` ("released without a recorded session close")
    for a worktree its killed first attempt had already removed, unless it was told it was a repeat.
  - `SnapshotAndRetain` and `SnapshotAndRetainRun` look for the snapshot an earlier attempt committed by
    its label only on a repeat, so a repeat returns that snapshot even when the tree moved since. Without
    it a snapshot of an unchanged tree commits nothing new (the tracker commits only staged changes), but a
    moved tree is committed again and the result names another revision. Measured on a repeat under
    another request id, each snapshot or retain adds one retained revision (effects 9 to 10, 7 to 8, 7 to
    8), where `EnsureWorkspace`, `RestoreRevision`, `DiscardWorkspace`, `CancelTurn`, `CloseSession` and
    the evaluation kinds add none. The label is `core:<request id>`, so the tag is what keeps it unique.
  - `EnsureSession` repeats safely only under the same request id: the binding records the ensure that
    made it, and a fresh-policy ensure under another id is refused ("session identity is already in use").
    The tag is stable across epochs, so the runner is safe; a retry that minted a new id would not be.
- **A job a step runner submits is registered for core's other job requests.** The evaluation executor
  answers `CollectEvidence`, `ObserveOwnedJob` and `CancelOwnedJob` only for a job in its registry, and
  `CloseAttemptScope` lists exactly the handles indexed under the scope. `JobStepRun` called the polling
  executor directly and wrote neither, so a collection was rejected as "not owned by this scope" and a
  close was released over a live job with an empty manifest. `JobRegistry` now owns both records, and
  `MeasurementRequests` and `JobStepRun` write through it before the executor is contacted. Core sends a
  step-run job a `CollectEvidence` when it ended with no evidence and a `CloseAttemptScope` at the
  attempt's closure, and no `ObserveOwnedJob` or `CancelOwnedJob`, since it only ever sees the job ended.
- **A pause leaves a Slurm job running.** `JobStepRun` used to cancel the job on any cancel signal, so a
  Pause threw away the queue time and the work, and the resumed run claimed a new job. A cancel that
  interrupts the attempt (`interruption_of`: `PAUSE_NOW`, `DRAIN_EXPIRED`) now returns `Cancelled` and
  leaves the job; `clear` adopts it and the next `run` attaches. A runner whose work dies with the host (a
  local evaluation) still cancels it. The `StepRunner` contract says it: after an interrupting cancel a
  runner has nothing running, or work `clear` adopts and the next run attaches to without starting it
  again.

- **A job step's report is its job's last observation.** A job runner answers a measurement
  submission with the job's terminal observation, evidence and facts, and no poll follows. Core takes
  that report as the job's observation too (`_step_attempts.finish`), so a run that is ending closes
  over a job it saw end, instead of waiting for a poll that a closing run does not ask for. A run that is
  ending asks to terminate the jobs it holds as the report lands, before the job's observation follows the
  report: a job its own report ended is not terminated (`_measurements._born_ended`), because the
  inspection core would open for an unconfirmed job is a step no runner serves.
- **A job step is held for the queue budget before its limit counts.** A job's limit counts from its start,
  and queue time (the scheduler's, or the root proof a run environment holds Slurm work for) is not the
  work's. Core sees a step's job only at its end, so no fact credits that time as the ledger did
  (`_wait_clock.credited`); `decide` instead starts a step whose lifecycle owns a job with
  `queue_wait_budget` added to its limit, never past the run's end (`_wait_clock.job_step_until`). The
  executor bounds the job once it runs (the scheduler's time limit, a stage's timeout).
- **A close lists the jobs the scope registered.** `decide` holds the `CloseAttemptScope` until every other step
  of its scope has a result ([Scope order](#scope-order)), and a pause never cancels an owned job. The job
  runners rely on both: the close cancels and lists exactly the handles registered under its scope, so a step
  must have registered its job before the close can read the registry, and a paused Slurm job is adopted by the
  resumed run instead of being cancelled and claimed again.
- **The snapshot kinds rely on `decide`'s scope order.** `RetainByContent` is safe once every writer of
  the scope has a result ([Scope order](#scope-order)); `ExecutorStepRunner` does not check it.
- **Per-environment `clear` (item 5e).** `RunEnvironment.agent_work_reaper(run_id, ...)` gives the reaper of
  one step's agent work, by the step's tag, which `AgentTurnStepRunner` takes. The run-wide `reap_orphans`
  would end every step's agent, so it is not a substitute. Each environment, and the step kinds a gap would
  leave unable to run there:
  - **Docker** sweeps the containers labelled with the run for processes whose initial environment carries
    the tag (`DockerAgentWorkReaper`, over `vs_agent.reap_step_agents`) and removes no container. The
    sweep prints how many processes it found, not how many it killed, and repeats until a sweep finds
    none, so a kill that fails is never read as "nothing there". It runs as the container's own user,
    which is the agent's: the agent image's `USER` is the agent and neither the agent's `docker exec` nor
    the sweep passes `-u`. A process's environment is readable only by its owner (and by root with
    `CAP_SYS_PTRACE`, which a default container lacks), so `-u root` would read nothing of the agent's.
    Residual: a process of another non-root user is skipped; the agent is never started as one.
  - **SkyPilot** runs the agent in the same editor container, so it gets that sweep, and then cancels the
    remote jobs the step's agent asked for. The premise that a SkyPilot evaluation is a step is wrong in
    code: no `SubmitMeasurement` runs on SkyPilot (or Modal); the agent calls the evaluator helper inside
    its turn, which sends the request over the host-owned bridge, and the bridge journals the invocation.
    So there is no evaluation runner to build, and `SlurmJobStepRunner`'s shape (a polling executor under
    a handle) does not fit. The helper sends the step mark its process inherited, the journal records it as
    `InvocationRecord.step_tag`, and `SkyPilotStepWorkReaper` cancels the tag's jobs by cluster and job id
    (`cancel_step_jobs`). The cluster is named per run and holds other steps' jobs, so it is never touched.
    A job whose id the host died before recording is found by its journaled name. Invocations journaled
    before this change carry no tag, so a rerun cannot cancel their jobs. Open: a request the helper sent
    just before it was killed can still be journaled after the sweep read the journal, and then its job
    runs until its own end; the window is the bridge's handling time of one request.
  - **Modal** has no per-step handle: its app is named per run (`VIBESYS_MODAL_APP_NAME`) and the
    candidate's `modal run` is a client process inside the agent container, so it carries the step's mark
    and is ended by the Docker sweep. What Modal then does with remote work whose client was killed is
    Modal's own cleanup and is not measured here: after a kill, a function call may keep running, and
    spending, until Modal ends it. A real-tier test with a Modal account is owed.
  - **Slurm** edits in a local Docker container too and gets the Docker sweep. Its jobs are adopted, not
    cleared (`SlurmJobStepRunner`). The ledger key names the content, not the run, so two runs on one
    cluster that ask for one content share one job, and a step can attach to a job another run
    submitted. A run's cancel goes through `relinquish`, which drops that run's lease and leaves the
    job to every other run that holds one, and `clear` itself cancels only the job of an attempt the
    ledger holds withdrawn, which no run wants by definition. A test pins that one run's cancel leaves a
    job another run wants. `clear` asks the tag's own handle first: a job on record is adopted whatever
    the workspace shows, and a handle on record whose plan cannot be keyed (the workspace lost the
    candidate's commit) is `ClearUnavailableError`, never "never ran". A withdrawn entry is recorded
    after the scancel was asked for, not after the job ended, so the scheduler is asked and the step is
    cleared only once no live job of that attempt shows; a reading that cannot tell is
    `ClearUnavailableError`. Residual: a submission a dead host had in flight when its attempt was
    withdrawn can still start a job under that name afterwards; the ledger's orphan check cancels it by
    name, and `clear` does not wait for that check.
  - **Host** finds the agent's processes through the same process table and reaper as a local
    evaluation's stage commands (`ProcessStageWorkReaper`). The mark an agent carries is the stage mark of
    the step's own handle (`agent_mark`), so one reaper finds both. `confine_to_sandbox(leads_group=True)`
    leads the host command with a shell that carries the mark as an argument (`step_shell_argv`), as
    `LocalShellRunner` leads a stage command, because macOS shows another process's arguments and not its
    environment. No step kind is refused: a Metal run on macOS starts and clears. A contract case runs the
    reaper over a process table that shows what Linux shows and one that shows what macOS shows, and an
    end-to-end case runs the production argv against the real `ps` or `/proc` of the host it runs on. The
    macOS residual below applies to an agent too: a descendant that left its group, or whose group leader
    died first, is not found.
  - **A call returns only when its thread work has ended.** The loop starts nothing on a tag until its
    `run` and `clear` have returned, and a task cancel is a nudge, so a runner that blocks in a thread
    must not return while the thread goes on. Every runner and reaper that takes a `BlockingRunner`
    shields it (`ShieldedBlockingRunner`): a cancelled caller waits for the function and then gets its
    cancel. That covers the agent turn runner's cancel, inspect and `forget_unfinished`,
    `SlurmJobStepRunner.clear`, and the Docker, SkyPilot, process and container reapers. The workspace
    layer and the executors shield theirs, and the workspace, adoption, operation and executor runners
    make no thread call of their own. `StepRunnerContract` holds a `DetachedBlockingRunner` (a Fake whose
    call outlives its caller, as a thread does) shut, cancels a `run` in its stop path and a `clear`
    mid-work, and fails if the call returned first; `StageWorkReaperContract` does the same for `reap`.
    The case fails if a runner offers a blocking runner and never calls it from `run` or `clear`. It
    runs on the local runner (both process tables), the agent turn runner and every reaper; Slurm's own
    thread work is on the ledger path, which a dedicated test cancels.
  - **Found on the way:** `SkyPilotEnvironment` inherited a Docker run-wide reap that read Docker settings
    its configuration lacks, so resuming a SkyPilot run raised `AttributeError`; and the Modal and Slurm
    environments never reaped their run-labelled agent containers on resume. Both are fixed, with a test
    over every container environment.
- **6a must replace the receipts the 13 executor kinds rely on.** In plain words: `EnsureWorkspace`,
  `RestoreRevision`, `DiscardWorkspace`, `CloseAttemptScope`, `SnapshotAndRetain`, `RetainRevision`,
  `EnsureSession`, `CancelTurn`, `CloseSession`, `SnapshotAndRetainRun`, `ObserveOwnedJob`, `CollectEvidence`
  and `CancelOwnedJob` are run again after a crash with no `clear`, and that is safe today only because their
  executors write a receipt for each request and read it back. The cutover deletes those receipts, so 6a has
  to give each kind an equivalent that does the same job. Two need it spelled out: `EnsureSession` repeats
  safely only under the same request id, and `SnapshotAndRetain` and `SnapshotAndRetainRun` find the
  snapshot an interrupted attempt already made by its label only when they are resumed.
- **The executor kinds repeat safely today because their executors keep receipts.** `ExecutorStepRunner`
  runs on `RuntimeWorkspaceRequests`, `RuntimeSessionRequests` and the evaluation executor, which replay a
  sealed result by request id and resume an unsealed one (`run_once`). Every kind passes a kill at sampled
  durable writes through the runner (every write, for `EnsureSession`), but the cutover deletes
  `run_once`, and these facts must then hold without it:
  - `SnapshotAndRetain` and `SnapshotAndRetainRun` look for the snapshot an interrupted attempt already
    committed by its label only when resumed (`_snapshot`, `find_snapshot` under `if resumed`). Without
    the flag a repeat commits again: an unreferenced equal-tree commit, never a wrong result. The lookup
    has to become unconditional. Measured on a repeat under another request id, each snapshot or retain
    adds one retained revision (effects 9 to 10, 7 to 8, 7 to 8), where `EnsureWorkspace`,
    `RestoreRevision`, `DiscardWorkspace`, `CancelTurn`, `CloseSession` and the evaluation kinds add none.
  - `EnsureSession` repeats safely only under the same request id: the binding records the ensure that
    made it, and a fresh-policy ensure under another id is refused ("session identity is already in use").
    The tag is stable across epochs, so the runner is safe; a retry that minted a new id would not be.
- **The snapshot kinds are unchecked here.** `RetainByContent` is safe once every writer of the scope has
  a result, which is `decide`'s rule (item 4a); `ExecutorStepRunner` does not check it.
- **Fold cost.** Measured, see [state.pb is a cache](#statepb-is-a-cache); the keyframe interval is open.
- **Stage work left running on a Metal run on macOS.** This applies only to Metal runs on the macOS host, where
  macOS shows other processes' arguments but not their environment. A stage command is found by the mark on
  its process group leader. If something outside the run kills that leader with SIGKILL while other processes
  of its group keep running, nothing visible carries the mark any more. `clear` does not find them and
  reports the host clean, and the re-run then races them. A child that starts its own session is missed the
  same way. Linux and Docker runs are not affected: Linux reads the mark from every process's environment,
  and Docker removes the container. No durable record of the group is kept; accepted for now. See
  [marks on local stage work](#marks-on-local-stage-work).
- **Fold cost.** See [state.pb is a cache](#statepb-is-a-cache).
- **Interim bodies.** Request and event bodies in the new records are not yet proto messages. Until phase 3
  generates them, schema evolution of those bodies follows today's envelope migration chain, which is
  deleted at phase 3.
