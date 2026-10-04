# vs-project

## Responsibility

This package owns the `.vibesys` filesystem contract for a repository-native
project. It discovers and validates tasks, binds project state to one repository
root, and provides access to generated run state. Applications should use one
`Project` per root rather than assemble paths or state stores themselves.

Orchestration-specific settings, resume rules, agent roles, and round-history
interpretation belong in the owning `src/vibesys/loops/<strategy>` package or
`src/vibesys/orchestration`, not in `vs_project`. Run manifests use a
versioned `OrchestrationDescriptor`; this package validates its portable JSON
envelope, while the owning orchestration validates the options and decides
whether a resumed run may change them. Version 5 is the only supported run
manifest format; older schemas fail explicitly.

## Usage

Application code uses task operations directly and persists state through
`project.state`:

```python
from vs_project.api import Project

project = Project.open(".")
task = project.select_task("latency")
manifest = project.state.load_project()
```

`Project.discover()` searches an existing path and its parents for the closest
`.vibesys/tasks` directory. `Project.open()` accepts any existing directory so
legacy inputs can use generated state without defining repository-native tasks.

The package owns the complete `.vibesys` filesystem contract:

```text
.vibesys/
├── tasks/<task-name>/
│   ├── OBJECTIVE.md
│   └── vibesys.input.toml
└── state/
    ├── project.json
    ├── runs/<run-id>/
    └── local/
```

Layout validation and persistence remain separate internal implementations.
The public package does not expose independently constructible layout or state
objects, which prevents application code from binding them to different roots.
State namespaces and immutable state value types remain public for integrations
that consume them.

## Atomic run state

`Project.state_store(run_id)` opens a `LocalStateStore` in the shared portable
run namespace, including before its manifest exists. `StateStore` is the role;
`FakeStateStore` implements the same contract in memory. Both accept a public
`fault_plan` of `CommitFault` values for definite and ambiguous commit failures.
Local ambiguity faults fail immediately before or after the actual rename,
exercising the same error classification and reload durability repair as a
filesystem failure.

The store treats serialized kernel envelopes as opaque bytes. State, request
outbox and event cursor belong inside one payload, encoded and decoded by the
kernel's public codec. `StoredEnvelope.revision` is an independent storage CAS
token, starting at zero and increasing with every replacement. It does not
replace the kernel revision inside the payload.

Acquire a `StoreFence` with a host identity, supplied time and positive lease
duration. Renewal preserves its epoch and never shrinks its expiry. An expired
lease permits takeover with a higher epoch; previous epochs cannot commit.
Time must use one durable shared basis across hosts, without regressions.

`commit(expected_revision, envelope, fence, now)` checks ownership and revision
under the same lock that publishes the complete record and lease document.
`None` expected revision means absence. Outcomes are:

- `Committed`: the complete candidate is durable.
- `Conflict`: no candidate was written; reload and reconcile the revision or
  reacquire host ownership.
- `Unknown`: dispatch is forbidden until reload resolves whether the complete
  candidate exists. Compare its storage revision and bytes. A later competing
  transition requires reconciliation rather than blind replay.

An injected definite failure raises `StateStoreWriteError` and writes nothing.
Lease write errors also grant no dispatch authority. Real observation errors
propagate. Local publication uses a stable shared `flock`, a temporary file,
file fsync, rename, and directory fsync. Reload synchronizes the observed file
and its directory ancestry before returning, resolving lost durability
acknowledgements. The shared filesystem must support these operations.

`quarantine(...)` performs the same fenced CAS with a `QuarantinedEnvelope`.
It preserves unmigratable source bytes and diagnostics and atomically replaces
the runnable record. An explicit `None` schema version preserves unknown source
metadata without fabricating a version. `load()` returns this explicit blocked alternative;
quarantine grants no dispatch authority.

Future shell composition must serialize every whole transition before I/O,
retain the storage CAS token separately from kernel state, map storage ownership
to the kernel host fence, renew and verify the shared lease, and suspend dispatch
on conflicts, unknown outcomes, write errors or quarantine. Restart must decode
through the authoritative kernel codec, migrate explicitly, and reconcile
persisted request identities before replay. External executors must honor the
epoch or provide identity-based idempotency and reconciliation; a filesystem
lease alone cannot stop an already executing external request.
