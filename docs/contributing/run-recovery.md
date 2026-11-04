# Run recovery

This page describes how a run continues after a crash, Pause, or host restart.
The log is the authority for core state; `state.pb` is a cache of that log.

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
   Ending a run cannot fail: cancel the work that is open, run the cleanup that frees what the run
   holds, wait for both, then end.
5. **A step that cannot conclude is bounded.** An executor that answers with an observation that is not
   final has given the step no result: the attempt raised, it is charged as a crash is, and nothing it
   left is adopted. The step runs again at most `request_retries` more times, each after a backoff on the
   run clock that doubles with every charged attempt up to `request_backoff_cap_seconds`, and then ends
   `Failed` (`ATTEMPTS_EXHAUSTED`), so a run whose far side never answers ends or halts on a typed stop
   and never waits on it, and a runner that fails at once does not spend every try in one instant. Two waits are not bounded by the loop: a `clear` that keeps failing,
   and an attach to work a cancel left running (`_end_alive`). Both show on the `StallBoard`. Pause now, a
   pause whose drain deadline passed (`drain_seconds`, which parks the run) and, for `clear`, an ending
   abandon the first. A plain drain, the operator's request to wait for the admitted work without bound, waits
   for a re-run's `clear` as it waits for any admitted work, until the operator escalates to Pause now.
   The second is abandoned only by a park: an ending waits for it, because a job still running holds the
   cluster, and the loop never records a step cancelled over work it cannot show ended. `vs-dst` holds its worlds to the rule
   (`test_a_request_the_world_never_answers_finally_is_asked_within_the_retry_bound_and_the_run_halts`).

## Steps not safe to run twice

Core classifies every request with `LifecycleClass`
(`libs/vs-core/src/vs_core/types/records.py:request_lifecycle`): `QUERY`, `IDEMPOTENT_WRITE`,
`OWNED_JOB` and `SESSION_TURN`. The loop treats all four alike: a step with no result runs again, after its
runner's `clear`. The table below keeps the four classes and adds what `clear` does for each.

| Steps | Effect | After a crash | `clear(tag)` | Key |
|---|---|---|---|---|
| `ObserveOwnedJob`, `CollectEvidence`, `VerifyAdoption`; registered `QUERY` operations (`interpret_evidence`, `verify_revision`, `export_patch`, board reads) | read only | safe | none | none |
| `EnsureWorkspace`, `RestoreRevision` | create or reset an attempt's workspace to a recorded revision | safe: the end state is fixed by the request | none | tag (workspace is keyed by attempt and generation) |
| `DiscardWorkspace`, `CloseAttemptScope`, `CloseSession`, `CancelTurn`, `CancelOwnedJob`, `EnsureSession` | release or bind a resource | safe: releasing twice is a no-op in the contract | none | tag |
| `SnapshotAndRetain`, `SnapshotAndRetainRun`, `RetainRevision` | commit the workspace and write a retention ref | safe, and `decide` makes it safe: it runs the step only when every writer of its scope has a result, so it never reads a half-written tree; the ref is content-addressed | none | revision id |
| `AdoptRevision` | write the winner into the operator's checkout | safe: it sets the target state and verifies it | none | tag |
| `DispatchTurn`, `ResumeSessionTurn`; registered `SESSION_TURN` operations | one agent turn: a process group, writes to the workspace, tokens, tool calls | safe after `clear` only | kill the processes carrying the tag in the run's containers (`vs_agent.reap_step_agents`; the containers belong to a role or a workspace and outlive the step, so none is removed), then, unless the journal holds an answer for the invocation, return the workspace to the baseline of its access receipt (`AccessSettlement.rewind`) and forget the invocation's unanswered journal record (`AgentSessions.forget_unfinished`), so the retry dispatches it again and resumes the last completed turn | tag |
| `SubmitMeasurement`; registered `OWNED_JOB` operations | an evaluation job on Slurm, Docker or a local process | Slurm: safe, the ledger finds the job. Docker and local: safe after `clear` | Slurm: adopt (request claimed, queued, running or ended); a pause or a drain deadline leaves the job running and `run` attaches to it; only a cancel that ends the step cancels it. Local and Docker: cancel the process's own evaluation, kill the stage process group, remove the labelled container, discard partial stage output | Slurm: the ledger's content key, which `clear` computes from the step. Local: the tag |
| registered `IDEMPOTENT_WRITE` operations: `render_artifacts`, `retain_revision`, the dynamic handoff write, issue-board `ApplyIssueChanges` and `LogProgress` | content-addressed or keyed writes | safe: each carries a key the tracker or store checks before writing | none | existing key |
| `capture_command`, `validate_local` | run a command or validation recipe in a throwaway candidate workspace | safe to rerun | none: the candidate is keyed by the request identity and replaced on the rerun | request identity |

The ledger keys a request by a hash of the content the job reads and its spec, not by the tag, so `clear(step, tag)` computes the key from the step: it opens a candidate worktree at the step's revision, hashes it, discards it, and reads the ledger. Two steps of one content therefore share one job, and a step that finds the job another step submitted attaches to it. A job that was cancelled (its cancel ended the step, or someone else cancelled it) is final in the ledger, so the next attempt asks for a new handle of the tag (`-life-n`) and the ledger claims a new job. A pause or a drain deadline cancels nothing: the job stays queued or running, `clear` adopts it and the next attempt attaches. A local evaluation never attaches: each run takes a fresh handle.

`InspectRequest`, `CancelOwnedResource` and `BlockRequest` are core's commands of reconciliation
(`COMMAND_KINDS`), which `decide` never runs as steps. `InspectTurn` and `InspectOwnedJob` are steps that no
runner serves (`UNSERVED_STEP_KINDS`), so a step loop run must never open one: its steps end with a final
report or none, and a job step's report is its job's last observation, so there is no outcome left to
settle. A property over the loop model (`opens_no_unserved_step`) holds core to it, and the router refuses
all five kinds (`RECOVERY_ONLY_KINDS`). The five kinds, and the reducers that open them, are still in
`vs_core`.

### The runner of each request kind

Every kind of `libs/vs-core/src/vs_core/types/records.py:Request` (24, in six groups) has one runner, or
none, because no run opens it. "Executor step" below means a step that runs on its role's existing executor and needs no
`clear` (the "none" rows of the table above); `ExecutorStepRunner` runs them.

| Group | Kind | Runner |
|---|---|---|
| Session | `DispatchTurn`, `ResumeSessionTurn` | `AgentTurnStepRunner` |
| Operation | `ExecuteRegisteredOperation` of lifecycle `SESSION_TURN` | `AgentTurnStepRunner`, through an `OperationTurns` the wiring supplies (`NoOperationTurns` in production: no production operation is a turn) |
| Operation | `ExecuteRegisteredOperation` of lifecycle `OWNED_JOB` | `SlurmJobStepRunner` or `LocalJobStepRunner` |
| Operation | `ExecuteRegisteredOperation` of lifecycle `QUERY` or `IDEMPOTENT_WRITE` | `OperationStepRunner` |
| Evaluation | `SubmitMeasurement` | `SlurmJobStepRunner` or `LocalJobStepRunner` |
| Adoption | `AdoptRevision`, `VerifyAdoption` | `AdoptionStepRunner` |
| Workspace | `EnsureWorkspace`, `RestoreRevision`, `DiscardWorkspace`, `CloseAttemptScope` | `ExecutorStepRunner` |
| Workspace | `SnapshotAndRetain`, `RetainRevision` | `ExecutorStepRunner`; they need their scope's writers ended first |
| Session | `EnsureSession`, `CancelTurn`, `CloseSession` | `ExecutorStepRunner` |
| Session | `SnapshotAndRetainRun` | `ExecutorStepRunner`; it needs the run's writers ended first |
| Evaluation | `ObserveOwnedJob`, `CollectEvidence`, `CancelOwnedJob` | `ExecutorStepRunner` |
| Reconciliation | `InspectRequest`, `CancelOwnedResource`, `BlockRequest` | none: commands, never steps |
| Session | `InspectTurn` | none: no step loop run opens it |
| Evaluation | `InspectOwnedJob` | none: no step loop run opens it |


## Core state is a fold of the log

`run.log` holds every input core consumed and every step result. Core state is a deterministic reducer
(`libs/vs-core/src/vs_core/_step.py:step`, from a state and an event to a state and requests), and
`fold(records, seed)` applies it to the log.

Field by field (`libs/vs-core/src/vs_core/types/kernel.py:CoreState`):

| State | How the fold gets it |
|---|---|
| `run` (facts, declaration, limits, requirements, capabilities, deadline, duration) | the `RunStarted` seed (`CoreSnapshot` for a migrated run) |
| `run.controls`, `run.receipts`, `run.result` | controls from `CoreInput` control records; receipts and result derived |
| `scheduling`, `attempts`, `sessions`, `evaluation`, `settlement` | derived from decisions, step results, controls and clock marks |
| `records.records` (the request ledger) | derived; `phase` collapses to open or finished, and attempts come from `StepStarted` |
| `records.epoch` | the host epoch of the log: `epoch_opened` advances it when a later epoch of the log opens |
| `strategy` and `event_cursor` | one `StrategyAdvanced` record per advance ([The search in the log](#the-search-in-the-log)); a resume on the same code replays the journal it holds |
| `pending_inputs`, `pending_decisions`, `wakes`, `pending_publications`, `delivery_cursor` | derived, except the owner events: the ledger derives the owner event of a report (`_request_forward.py`) but not every fact an executor tells an owner (a turn's reply, a refused mid-turn steer, a job's failure class, an adoption's publication), so those travel in the `StepFinished` record. Decisions and wakes are the strategy's output; publications are the fold's output. The delivery cursor is the one fact that is not derived: after a restart every publication since the last recorded acknowledgement is delivered again |

Outside the log, executors keep durable state keyed by tag: the provider session ids
(`vs_agent/session_store.py`), the Slurm ledger on the cluster, workspace git state, and the keyed
documents of `vs_runtime._keyed_documents`. None of it is core state. A runner reads it only inside
`clear` and `run`.

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

The log is the authority and `state.pb` is a cache of it.

- A commit is one append of one atomic record (`vs_runlog` already frames, checksums and drops a torn
  tail; `RunLogReplayStore` is the precedent). There is no second write to wait for.
- `state.pb` is written at pause, at end and at keyframes with a temp file, an fsync and a rename. It
  records the log sequence it covers. It is never compared and swapped and never authoritative.
- On start the loop reads `state.pb` if it parses and its sequence is not past the log; it then folds the
  records after that sequence. If it does not parse or is ahead of the log, the loop folds the whole log.
  Deleting `state.pb`, keyframes and `run.head` changes no result.
- `committed_seq` in `run.head` keeps its meaning: the gateway serves records up to the last durable one.


## Pause

The pause drain stops `Run` for new steps, waits until each running
step ends or the drain deadline passes, records `Parked`, and exits. Three facts about it. A step cancelled by Pause now is recorded as `AttemptInterrupted(PAUSE)`, not as a result, so Resume
runs it again as the next attempt. An attempt a pause interrupted is neither a policy failure nor a retry debit; one whose runner raised is charged. The drain deadline is a field of the pause input,
not a constant in the loop.

## Legacy reconciliation

- **The kernel's restart reconciliation.** `RecoveryStarted`, `RecoveryReady`, `RecoveryCheckWaitLapsed`
  and `open_host_epoch` (the host log and its downtime credit) are handled in
  `libs/vs-core/src/vs_core/_request_recovery.py` and `_step.py` (`_resume_epoch`, `_recover_writer`). The
  step loop does not use them: a restart goes through the log (the `EpochStarted` header, `epoch_opened`,
  then `clear`), and a grep over `src` and `libs/*/src` finds no code that constructs `EpochResumed` or
  `RecoveryStarted` outside the kernel's own types and the test drive. The code stays, and
  `libs/vs-core/tests/test_request_recovery.py` and the vs-core drive exercise it.
- **The rest of the prove-or-block layer in `vs_core`.** The inspection, ownership and release-proof
  branches, `ObservationStatus.UNKNOWN`, and the
  five request kinds `InspectRequest`, `InspectTurn`, `InspectOwnedJob`, `CancelOwnedResource` and
  `BlockRequest`. They are reached from events a step loop run does not produce (`RetireRequested`,
  `ScopeReopenAdmitted`, `ReacquisitionReady`, the restart reconciliation); no simulation tier enters them.
  The envelope no longer holds a fence (version 46); a retained run's fence receipts are read by their own frozen model.
