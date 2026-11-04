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

## Legacy run state

A run saved before the step loop kept its whole run envelope in `store.json`, as one record,
together with the lease of the host that wrote it. Nothing writes that file any more: the run's
state is its run log. `Project.state_store(run_id)` opens a `LocalStateStore` in the shared
portable run namespace, including before its manifest exists, and `FakeStateStore` implements the
same role in memory. `StateStore` is the role, and it only reads: `observe()` takes the atomic
published bytes without a lock, and `load()` takes them under the store lock after synchronizing
the document and its namespace links through the existing project root, so nothing is read as
durable that was not made durable. Both return a `StoredEnvelope`, a `QuarantinedEnvelope` (a
migration that was blocked, with no authority to run) or `None`.

The store treats the envelope as opaque bytes: the kernel's public codec decodes it, and the
migration into the run log (`vibesys.run.store_migration`) is its one reader. `StoredEnvelope.revision`
was an independent storage token, starting at zero. The document is either version 1, which also
holds the writer's lease and which a reader accepts and ignores, or version 2, which moved the
lease to a `lease.json` that is never read. A damaged document, or one from a newer release, is a
`StateDocumentDamagedError` that names the file and both versions and never echoes its bytes.
`observation_fault_plan` accepts `ObservationFault.READ` and `.SYNC` for read and directory fsync
errors; a `None` entry lets that read complete normally. A test puts a record in place with
`seed_stored_record` from `vs_project.api.testing`, the only writer left.

## Git history

`GitTracker` owns snapshot policy: which paths are framework state, how a
checkpoint is committed, which paths are excluded. It does no I/O of its own.
Every Git operation goes through `GitRepository`, a role interface of semantic
operations (`head`, `is_ancestor`, `stage_all`, `commit`, `diff_patch`, ...)
declared in `vs_project.api` with typed errors (`GitError`, `GitCommandError`,
`StagingError`) and typed values (`RepositoryLocation`, `CommitSubject`,
`PatchStyle`). Its module docstring is the contract: conventions, pathspec
subset, atomicity and restart expectations, and which operations are
CLI-only.

`CliGitRepository` runs the Git CLI and is the reference implementation;
`GitTracker(..., repository=...)` accepts any other. The contract suite in
`tests/git_contract` runs every case against every implementation registered in
its `conftest.py` (`IMPLEMENTATIONS`), including operation sequences compared
with the CLI as the oracle. A new implementation is one entry there.

`Pygit2GitRepository` answers the portable operations in process with libgit2
(`pygit2`) and delegates the CLI-only ones to a wrapped `CliGitRepository`; its
module docstring lists the exact split, the requests it hands to the CLI to keep
Git's answer, and the known differences. It shares a repository safely with the
real `git` the agents run (the contract suite alternates the two). Which
implementation a `GitTracker` gets is decided in one place,
`open_git_repository`: `VIBESYS_GIT_BACKEND=cli|pygit2` selects explicitly (an
unknown value, or `pygit2` where the package is not installed, is a
`GitBackendError`); unset, it is `DEFAULT_GIT_BACKEND` when `pygit2` is
installed and the CLI implementation otherwise. `pygit2` is imported only when
that backend is built.

Repositories that are not the tracked project (cloning a source tree, probing
whether a directory is inside a repository, remote operations in
`GitRemoteRepository`) are outside this interface and use `run_git` directly.
