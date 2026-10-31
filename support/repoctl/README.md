# repoctl

`repoctl` plans work from repository changes and runs selected checks. It reads
`.repoctl/components.toml` for ownership, discovery, and dependency policy and
`.repoctl/checks.toml` for check groups and native commands. It discovers
components, maps changed paths to their owners, and walks reverse dependency
edges. The Go program contains no repository-specific paths, jobs, or
commands; those belong in the policy.

The wrapper invokes the repository's Go toolchain. Install Go 1.25, the
version declared by `go.mod`, before running it.

From a repository root:

```sh
./support/repoctl/repoctl test                    # plan and run selected checks
./support/repoctl/repoctl test --dry-run          # show selected commands
./support/repoctl/repoctl plan --base main --head HEAD
./support/repoctl/repoctl explain path/to/file --json
./support/repoctl/repoctl validate
./support/repoctl/repoctl verify-policy --cases tests/repoctl/cases.toml
./support/repoctl/repoctl run-checks --group python_quality
```

`test` and `plan` accept `--base`, `--head`, and `--event`. `test` also includes
staged, unstaged, and untracked local paths; `plan` uses only committed changes
for CI. Pull requests compare against the merge base; pushes use the exact
endpoints. By default, repoctl loads `.repoctl/components.toml` and
`.repoctl/checks.toml`. `--config` accepts a repository-relative or absolute
policy directory, or a legacy single-file policy. `REPOCTL_ROOT` can set the
repository root explicitly. CI uses `plan --github-output PATH` to emit
configured job and collection names, then runs named `[[check_groups]]` from
`.repoctl/checks.toml` independently with `run-checks --group NAME`. Groups can
have ordered commands, environment overrides, and selected package
collections. `run-native --targets-json JSON` executes only registered native
targets from a plan. `test` runs two kinds of group for the jobs that plan
selects: `include_in_test` (the test groups and native targets) and
`include_in_checks` (the quality groups). An `include_in_checks` group with no
`trigger_job` runs on every change; that is the always-on scripts.
`--checks-only` runs only the `include_in_checks` groups.
`test` plans with a narrower local view than CI (`plan`, `run-checks`), so a
local run stays short while CI keeps its full selection. A component with
`local_jobs` selects only those jobs in `test` instead of all jobs and
collections (the repoctl policy selects only `impact`). A component root in
`local_ignore_roots` stays owned for the CI plan and is ignored by `test`.
An entry of `roots` or `files` may also be a table, `{ path = "dir", selects = false }`:
the component lists it, for whatever reads the list (a content key, say), but a change under it
selects nothing, in CI or in `test`. One list carries both tiers, so a path is never written
twice; the path must still exist, and a path cannot be listed as both kinds.
A check group with `local_paths` or `local_components` runs in `test` only when
a changed path is under one of the paths, or under a root or file of one of
the named components (declare a list that CI also selects on once, in
components.toml, and name its component). A group with `local_skip` does not
run locally. `test` prints each group it skips, including the `local_skip`
reason.
Pass `--collection-json '["package-name"]'` to `run-checks` for a group with
a configured collection; unknown or duplicate values fail before execution.
In GitHub Actions, `plan --github-event` reads `GITHUB_EVENT_NAME` and
`GITHUB_EVENT_PATH` and rejects malformed or unsupported events before
publishing outputs.

For CI plans, repoctl reads the policy and discovered components from both
the comparison base and head revisions. Added paths use head ownership,
deleted paths use base ownership, and modified or renamed paths can select
both historical and current jobs. The plan's runnable components and package
collections come from the head policy, so removed package values are not sent
to current check jobs. A base job is included only if the head policy still
defines that job.

## Architecture boundary

The repoctl core must remain language agnostic. It must not branch on language
names or import language implementations. Language-specific discovery belongs
behind `discovery.Adapter`; check validation and planning belong behind
`execution.Planner`. Register implementations only in the composition files:
`discovery_adapters.go` and `test_planners.go`. The architecture test checks
this boundary.

Git revision comparison, worktree status, and snapshot lifecycle live in the
private `internal/gitrepo` package. Command execution is shared through
`internal/command`; repository planning consumes Git changes without owning
Git subprocess details.

## Extension contracts

The shared discovery contract is [discovery/discovery.go](discovery/discovery.go).
Each `discovery.Adapter` receives the repository root and one `discovery.Spec`,
then returns `[]discovery.Component` or a descriptive error. Components declare
slash-separated repository-relative ownership paths, opaque IDs, dependency
IDs, jobs, and optional language/target metadata. The graph validates IDs,
paths, dependencies, and cycles, then selects affected components. Adapters do
not walk the graph or run tests. Implementations live in
`discovery/python/`, `discovery/typescript/`, and `discovery/native/`.

The shared execution contract is [execution/execution.go](execution/execution.go).
An `execution.Planner` validates a check group for its language and turns
selected collection values into ordered `execution.Check` records. A check
contains an argument-vector command, working directory, timeout, and environment. `execution.Runner`
executes checks and reports failures. Language planners live in
`execution/python/`, `execution/typescript/`, `execution/golang/`, and
`execution/rust/`. TypeScript expands selected workspace packages; Go and Rust
native targets run their configured commands within each selected manifest
root. Python currently runs its configured full suite for a selected job.

`repoctl test` runs the affected `include_in_test` and `include_in_checks`
groups. It is not a command to run every CI group. Groups that cannot run
locally set `local_skip` (Docker for the real-system tiers, a display for
`clients_e2e`) and `--dry-run` names them. The
[contributor guide](../../docs/contributing/development.md#ci-gates)
documents the explicit `run-checks` command for `clients_e2e` and the required
uv, client-workspace, and Playwright Chromium setup.

To support a new manifest format, add a discovery adapter, register it in
`discovery_adapters.go`, and configure it in the repository policy. To support a new test
selection scheme, add an execution planner, register it in `test_planners.go`,
and configure its command arrays in the policy. Shared policy validation and
check execution dispatch through these contracts. The graph walker remains
language-independent. The native manifest
adapter discovers Go and Cargo roots; cross-root edges are declared in the
policy, rather than inferred from Go or Cargo metadata.

The policy rejects unknown keys, unowned changed paths, unsafe paths, missing
dependencies, cycles, conflicting manifests, invalid commands, jobs without
runnable groups or targets, and unregistered in-scope native manifests.
Commands are argument arrays, with no shell expansion. Invalid selected
collections and missing check groups fail instead of silently skipping checks.

`verify-policy --cases FILE` checks expected jobs and collections for
repository-owned scenarios in a separate TOML file. CI runs it before
publishing a plan. Each case names `paths`, exact `jobs`, and every configured
collection under `[cases.collections]`. A collection expectation accepts:

- An explicit string list (including `[]`) when the exact set is the contract.
- `"all"` for every eligible value in the discovered graph's collection.
- `{ dependents_of = "component-id" }` for the named component and all its
  transitive dependents, projected into that collection. Use the full graph ID,
  for example `pnpm:@vibesys/proto`, rather than a package name alone.

Intent expectations use the same discovered graph and collection eligibility
rules as selection, but resolve independently of changed-path selection.
Adding a package or dependency therefore needs no edit to an intent case.
Verification still requires exact equality and names missing and extra values;
unknown intents, keys, and component IDs fail validation.

Generic tests generate fake graphs and compare selection
against an independent reachability oracle. Bounded fuzz runs cover graph
selection and Git diff parsing when the tool or its policy changes.

To work on the tool itself, run `(cd support/repoctl && go test ./... && go vet ./...)`.
