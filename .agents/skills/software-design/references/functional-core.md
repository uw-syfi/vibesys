# Functional core, interfaces and implementations

Apply [rule 14](../SKILL.md#rules) to stateful orchestration, workstream and
hypothesis lifecycle, scheduling, evaluation lifecycle, and resource release.
Keep decisions pure so event sequences and crash recovery can be stress-tested
without I/O. Test I/O semantics separately, so failures can be traced to core
logic or an implementation.

## Vocabulary

- **requests** are typed data the core emits to ask for I/O. Typed outcomes
  return to the core as events.
- An **interface** is a `typing.Protocol` declared in the `.api` of the library
  that owns that kind of I/O. Name it by role, with no suffix: `Cluster`,
  `StateStore`, `AgentSessions`, `Sandbox`, `Evaluator`, `IssueTracker`. This
  matches existing names such as `AgentSessions`, `EvaluationStore`, and
  `Clock`.
- An **implementation** fulfills an interface. Name it `<Variant><Role>`:
  `SlurmCluster`, `FakeCluster`, `DockerSandbox`, `ModalSandbox`.
- The **shell** is the async driver that runs the core and executes its
  requests through interfaces.

Avoid these names and terms for these concepts:

- A `Protocol` suffix: redundant; "protocol" means wire protocols here, such as
  `vs-evaluator-protocol`.
- `I` or `Interface` prefixes: redundant with the declared interface type.
- "provider": means agent CLIs.
- "driver": means `vs-agent`'s agent drivers.
- "effects" or "handlers": say requests and implementations.
- "ports" or "adapters": say interfaces and implementations.

Rename existing outliers `AgentClientProtocol` and `ComputeBackendImpl` when
touched.

## Core shape

Use `step(state, event) -> (new_state, requests)`. Do not mutate the input.
State contains values and durable intent, never tasks, clients, or callbacks.
Time, deadlines, generated identities, and random choices arrive as validated
input data. No I/O, clock reads, randomness, or asyncio belongs in the core.

The core owns legal transitions, admission, scheduling, retry bounds,
settlement, and release dependencies. Pure strategy functions and configuration
supply validated choices and bounds. A shell or implementation must not decide
whether a workstream succeeded, should retry, or may release a resource.

Use closed typed unions for events, requests, and operation-specific outcomes.
Include explicit `Unknown` for ambiguous observations, such as transport loss
after submission or a missing exit code. Preserve evidence and external
identity for reconciliation. Match exhaustively. Never infer success from a
normal return, absent exception, empty result, or `exit_code or 0`.

## Durable intent

1. Produce a transition with stable request identities, payloads, ownership,
   and pending intent. Atomically persist the new state and its intents before
   executing any request. A failed or ambiguous commit authorizes no dispatch.
2. Execute with the recorded identity. Make execution idempotent; reusing an
   identity with another payload is a typed conflict. An external system that
   cannot deduplicate needs inspection and reconciliation before replay.
3. Feed each typed outcome back as an event. Atomically record completion,
   resulting state, and successor intents together. Never commit one logical
   transition in pieces.
4. On restart, load durable state and replay unfinished intents before admitting
   new work. Inspect external identity first. Lost acknowledgement stays
   `Unknown` until reconciled; unsafe replay becomes an explicit unresolved
   state. Duplicate observations must not settle work twice.

This promises one logical completion, not exactly-once external execution.
Cancellation and release are resumable transitions too: record intent before
cancelling, and keep ownership until termination is confirmed or explicitly
unresolved. Use `vs_project.api.Project` to open persistence and workspace
layout, never rebuild its paths.

## Crash consistency

Recovery bugs cluster where the core meets an external system, so these rules
make recovery run on every start and keep one path for each fact.

- **One fact, one event; recovery is replay.** Each external fact enters the
  core as exactly one event or atomically recorded record. Live execution,
  post-restart inspection, and replay feed it through the same step. There is
  no separate recovery reducer, and an inspection result carries everything a
  live result does (the durable observation and the owner events with the
  reply). A projection between two record types either copies each field or
  lists it as omitted; a property over the fields checks that.
- **Crash-only startup.** Startup always runs recovery; a fresh run is recovery
  over an empty log. Stop, cancel, deadline, and crash converge on one path,
  so the recovery path runs every time and cannot rot.
- **Single commit point.** One logical transition (observation, owner events,
  successor intents) is one atomic durable record. The shell API accepts only
  whole records, so committing in pieces is impossible by the API's shape, not
  by convention.
- **No orphan waits.** Every waiting entity (a dispatched intent, an executing
  session, a recovery check) has an outstanding request or timer that will
  produce the event it waits for. A pure function derives the waits and their
  producers from core state; tests check it after every step and startup
  checks it, so a stall fails where it is created, not at the run deadline.
  Match every phase exhaustively so a new phase must name its producer.
- **Recovery is idempotent.** A crash during recovery followed by a restart
  converges. Tests crash inside recovery.
- **Durable effects have durable guards.** Volatile state never guards a
  durable effect: a fence held only in memory is lost on restart. Replace
  files atomically (write a temp file, fsync it, rename, fsync the directory),
  never rewrite in place. Check the ledger's consistency on load, like fsck.
- **Fencing epochs.** A restarted host takes a new epoch, recorded durably.
  Executors and external writes carry it and reject a stale one, so a still
  running old host cannot write.

Limits: these rules do not encode an external system's semantics, which come
from real runs (see the testing skill's
[fakes-and-contracts.md](../../testing/references/fakes-and-contracts.md)).
Python checks protocol order only at runtime. Bounded waits remain policy, but
"unconfirmed" must be a value the caller handles.

## Outcomes say what is known

Give each operation on an external system a closed union with a variant for
every real state of that system, including intermediate ones. Cancel is
`Confirmed | Requested(job_id) | Lost`, not done or error: a cancelled Slurm
job stays COMPLETING for 30 to 40 seconds and reports as running, and an
outcome with no "requested, unconfirmed" variant forced an error that ended a
user-stopped run. Match unions exhaustively with `assert_never`; `ty` enforces
it. Parse, do not validate: the boundary parses a response into a proof type,
and guards accept only proof types, never raw caller-supplied fields. See
[python.md](python.md).

## Interface contracts

Follow [rule 3](../SKILL.md#rules): a caller written once must stay correct for
all implementations, including failures. Prefer narrower roles to methods an
implementation silently skips. Docker and Modal containers, or real and Fake
Slurm execution, must satisfy the same role contract to be interchangeable.

Specify typed errors and outcomes, strict validation, stable identity,
idempotence, cancellation, and inspection semantics. Caller task cancellation
is not proof of external termination. Preserve partial evidence on failure.
Implementations classify observations; the core decides lifecycle consequences.
Bounded transport retries may live inside an implementation, but lifecycle
retry policy and its durable intent belong in the core. See
[boundaries.md](boundaries.md).

Ship one contract suite per interface in its owning library. Run it against
every implementation, Fake and production, with no implementation-specific
skips that hide weaker semantics. See the testing skill's
[fakes-and-contracts.md](../../testing/references/fakes-and-contracts.md).

## Shell and library placement

The shell serializes inputs, runs the core, persists transitions, maps requests
to interface calls, and feeds results back as events. It manages async tasks,
time delivery, and signal scopes, with no lifecycle decisions. It does not
change requested admission, retry, settlement, or release policy.

There is no separate library for interfaces. Each top-level I/O library owns
its interface in `.api` and its implementations internally or in smaller
libraries it fans out to: `vs-project` owns filesystem and state persistence,
`vs-slurm` cluster jobs, `vs-agent` agent sessions, `vs-sandbox` containers,
`vs-evaluation` evaluation execution, and `vs-github` GitHub operations.

Declare tach edges in the same change: shell -> core and shell -> each owning
library's interface. Implementations stay with or below their owning library.
The core imports no I/O library or interface. Shared pure value contracts may
live in pure libraries; do not import mixed I/O APIs for their types. Preserve
one-way dependencies and the [placement rule](../../../../docs/contributing/architecture.md).

## Worked example: HostCore

Read [HostCore](../../../../src/vibesys/orchestration/dynamic/control/core.py)
and its [property tests](../../../../tests/vibesys/orchestration/dynamic/test_host_core.py).
Submitting two workers with one slot starts the first and queues the second.
A `WorkerFinished` event releases the first slot and emits `StartWorker` for
the queue head. `StopRequested` prevents new starts. Time arrives in `at_s`;
slot and budget decisions need no async execution.

`StartWorker` is a request in this vocabulary. HostCore currently mutates
process-local state and uses older output names; it demonstrates the decision
boundary, not the full immutable or durable contract. For a durable version,
return new state with the start intent, commit it through `StateStore`, execute
the start through `AgentSessions`, and feed the typed observation back. Restart
from the committed intent and reconcile the same worker identity before replay.

## When not to use it

A stateless transform needs only a pure function. A one-shot script with no
resumable lifecycle may call interfaces directly. Do not add an intent ledger
or async shell when there is no stateful transition to recover.

## Red flags

- Lifecycle decisions inside implementations or the shell.
- Multi-step transitions committed in pieces, or I/O before durable intent.
- A request whose outcome never returns as an event.
- `or 0` or other defaults that turn missing evidence into success.
- Implementation kind checks or I/O imports in the core.
- Inspection or replay rebuilding a record through a different path than live
  execution (see [red-flags.md](red-flags.md)).

See [red-flags.md](red-flags.md) for substitution and module-shape checks.
