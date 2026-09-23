# repoctl

`repoctl` plans work from repository changes and runs selected checks. It reads a
repository-owned TOML policy, discovers components, maps changed paths to their
owners, and walks reverse dependency edges. The Go program contains no
repository-specific paths, jobs, or commands; those belong in the policy.

From a repository root:

```sh
./support/repoctl/repoctl test                    # plan and run selected checks
./support/repoctl/repoctl test --dry-run          # show selected commands
./support/repoctl/repoctl plan --base main --head HEAD
./support/repoctl/repoctl explain path/to/file --json
./support/repoctl/repoctl validate
```

`test` and `plan` accept `--base`, `--head`, and `--event`. `test` also includes
staged, unstaged, and untracked local paths; `plan` uses only committed changes
for CI. Pull requests compare against the merge base; pushes use the exact
endpoints. `--config` accepts a
repository-relative or absolute policy path. The directory containing an
absolute policy is the repository root; `REPOCTL_ROOT` can set it explicitly.
CI can use `plan --github-output PATH` to emit configured job and collection
names, then run jobs independently. `run-native --targets-json JSON` executes
only registered native targets from a plan.

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
An `execution.Planner` turns a configured suite and selected collection values
into ordered `execution.Check` records. A check contains an argument-vector
command, working directory, timeout, and environment. `execution.Runner`
executes checks and reports failures. Language planners live in
`execution/python/`, `execution/typescript/`, `execution/golang/`, and
`execution/rust/`. TypeScript expands selected workspace packages; Go and Rust
native targets run their configured commands within each selected manifest
root. Python currently runs the configured suite for a selected job.

To support a new manifest format, add a discovery adapter, register it in
`policy.go`, and configure it in the repository policy. To support a new test
selection scheme, add an execution planner and configure its command arrays in
the policy. The graph walker remains language-independent. The native manifest
adapter discovers Go and Cargo roots; cross-root edges are declared in the
policy, rather than inferred from Go or Cargo metadata.

The policy rejects unknown keys, unowned changed paths, unsafe paths, missing
dependencies, cycles, conflicting manifests, invalid commands, and
unregistered in-scope native manifests. Commands are argument arrays, with no
shell expansion. A selected job without a configured suite or native target
fails instead of silently skipping checks.

To work on the tool itself, run `(cd support/repoctl && go test ./... && go vet ./...)`.
