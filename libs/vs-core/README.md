# vs-core

Pure lifecycle values and `step(state, event) -> Transition`. The async shell
calls `Strategy.decide(project(state))` separately and commits the envelope
before executing requests. Import only `vs_core.api`.

Wave 0 publishes strict immutable schemas, dispatch, capability validation,
operation codecs, request registration and revision ownership. Area reducers
raise `KernelNotImplementedError` with their owning lane until wave 1 implements
them. No existing dynamic execution path is moved or activated here.


Persisted schema versions are explicit. Unknown or changed operation, envelope,
or strategy state schemas are rejected and require a caller-supplied migration
before decoding. `OperationMigration` selects a pure conversion explicitly and validates
its registered target. A stored run envelope of an older supported version is brought to
the current one by the chain in `vs_core._envelope_migrations` (`upgrade_envelope`), so an
update never forces a fresh run. Legacy dynamic-state
conversion and its golden fixtures belong to W0B.

`ProposalSubmitted(decisions, expected_revision)` consumes an ordered proposal
batch against one observed revision. Each decision commits or rejects in order;
one rejection rolls back that decision's speculative changes without undoing
accepted predecessors. A top-level event increments the core revision once.
`DecisionSubmitted` remains the singleton form. Identical decision identities
suppress callback redelivery in both forms, including stale batch replays.

Decision dependencies identify semantic completion, not the first dispatch
request list. Every causal request persists its `decision_id` and
`decision_dependencies`, including successors of later observations and queued
admissions. `DecisionReceipt.request_ids` records the cumulative causal requests;
`Accepted.request_ids` is the immutable acceptance-time snapshot. Owning reducers
emit internal `DecisionCompleted` after their lifecycle completes. The kernel
persists finality, rejects failed dependents and sends `DecisionDependencyResolved`
to records. `DispatchAuthorized` requires successful completion of all explicit
request and decision dependencies.

Registered operations enter records as `RequestPrepared`. Custom turns require
a registered pure normalizer to `TurnSpec`; records forwards
`RegisteredTurnRequested` to sessions. Owned jobs declare `resource_pool` and
use `RegisteredJobRequested` plus `RegisteredOwnedJob`, without a synthetic
measurement plan. Revision mutations declare `RevisionAuthority` and use
`RevisionOperationRequested` and typed `RevisionOperationObserved` acknowledgements
under attempts. The kernel permits execution requests only from the declared
owning area. Ordinary queries and idempotent writes remain records-owned.

The shell supplies `operation_schema` on registered `RequestObserved` values and
calls `OperationRegistry.validate_event` before `step`. Records uses
`operation_result(operation_id, observation)` to forward the registered concrete
outcome to `Strategy.on_event`. `OperationResult` restores the owner subtype from durable `outcome_json`. Typed payload and schema proofs
must match before an observation or callback can enter the kernel; arbitrary
models and copied mismatches are rejected. Envelope decoding restores typed
record outcomes through the same registry. Registered payload and strategy
schemas reject mutable containers/defaults and open types; canonical wire values
sort frozensets while preserving tuple order.

`Transition.requests` contains newly prepared outbox proposals. Repeating an
identical request does not reemit it. After committing `DispatchAuthorized`, the
shell executes the durable request selected by that event's `request_id`, not a
newly emitted copy. A prepared proposal alone does not authorize I/O; recovery
and bounded retries retain the recorded request identity.

Pause and resume cannot reverse closing or blocked runs, or a committed stop
result. While a run is paused core takes only a `Finish` or a `Withdraw`; the
shell holds every other decision the search makes (`deferred`) and submits it on
the resume, so a pause never reaches the search as a refusal. A pause holds
only that intake (and queued starts): work already admitted carries on and what
it yields is delivered while paused (`continues_admitted_work`), such as a
turn's evaluation call and wait, a job's end, or the resume of a wait whose
jobs ended. `RunDrained` defers final publication while attempts, sessions, jobs,
records or child ownership remain unresolved. Evidence and assessment kinds are
closed, and eligibility requirements retain exact provenance, purpose, revision
and source ownership attribution.
