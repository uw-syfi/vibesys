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

See [red-flags.md](red-flags.md) for substitution and module-shape checks.
