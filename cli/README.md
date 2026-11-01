# vibesys-cli

`vibesys-cli` is a standalone Go binary. The VibeSys optimization skills call it for anything that
must be deterministic: measuring a commit, comparing results, promoting a winner, and the
lifecycle of the jobs that do that work. The skills hold judgment (what to try, when to stop); the
CLI holds determinism. It does not launch an agent.

The Python package installs a different command, `vibesys`, and `vibesys compare` means something
else there. The Go binary keeps the name `vibesys-cli`.

Work is landing in stages. The `cmd/vibesys-cli` entrypoint prints its version and nothing else
yet. The module so far is the foundation later pieces build on: the layout, the layer rules, the
contract, the domain types, the interfaces, and the two `Store` implementations with the contract
suite they share. Rows marked "planned" below do not exist yet.

## Layout

| Path | Role |
| --- | --- |
| `cmd/vibesys-cli/` | Argument parsing and output. Nothing else. |
| `internal/core/` | Pure domain types and rules (job keys, state machine, cache reuse, verdicts, promotion checks): no I/O, no clock, no goroutines. |
| `internal/store/` | `Store`: the logical interface to results, events, logs, job state and plans. No paths, no formats. |
| `internal/store/ledger/` | The decisions every `Store` shares (what a submission joins or reuses, numbering, what a value may hold), as a pure `Ledger`, and the `Engine` that runs one over a `Backend`. It asks `core` whether a transition is legal (`Job.Apply`) and whether a submission joins a job (`CanJoin`). |
| `internal/store/memstore/` | In-memory `Store` for tests. The same `Engine` as `filestore`, with a backend in memory. |
| `internal/store/filestore/` | The only code that knows the on-disk layout. Owns migrations. Its package comment states the layout, the commit protocol and the filesystem operations it relies on. |
| `internal/store/filestore/fsys/` | The slice of a filesystem `filestore` uses, the operating system's implementation, and an in-memory one that models what a crash keeps and fails or dies at any write step. |
| `internal/store/storetest/` | The one contract suite every `Store` passes, and a differential driver that compares stores. |
| `internal/environment/` | `Environment`: where a job's commands run (`host`, `docker`). |
| `internal/backend/` | `Backend`: facts about the compute hardware a task targets. |
| `internal/wire/` | Planned: translation between `core` types and the generated contract types. |
| `internal/archcheck/` | The layer and layout rules below, as a check `go test` runs. |
| `gen/` | Generated Go for `proto/vibesys/cli/v1`. Never edited by hand. |

## Layer rules

Dependencies point one way: `cmd` and the shell packages, then interfaces, then `core`.

- `core` imports nothing from this module, only the standard library.
- `store`, `environment` and `backend` import `core` only. They declare an interface and a contract;
  implementations live in sub-packages (`filestore`, `memstore`) or later packages and import their
  interface's package.
- `ledger` imports `core` and `store`. `memstore` and `filestore` import `ledger`, and `filestore`
  its `fsys` seam; they never import each other. The contract suite `storetest` imports `core` and
  `store` only, and only test files import it (and `filestore`'s tests, `memstore` to compare with).
- `wire` is the only package that imports both `core` and `gen`. Domain types, the generated
  contract types and the on-disk encoding are three separate models; none of them is another's
  representation.
- `gen` imports nothing from this module.
- Storage layout (state directory names, file names, the per-user index location) appears only in
  `internal/store/filestore`. Every other package reads and writes through `Store`.

`internal/archcheck` enforces the import rules and the layout rule. A new package needs an entry in
its rules table in the same change, so an unlisted package fails the test instead of passing
unchecked.

## Contract

The contract is `proto/vibesys/cli/v1/`: the daemon's gRPC service, the job, result, verdict,
environment, event and plan messages, typed refusals, and the version handshake. The same messages
are the `--json` output of every command, in protobuf's canonical JSON form with the original field
names. Generated Go lives in `gen/`.

Regenerate it from the repository root:

```sh
buf generate --template buf.gen.cli.yaml
```

`./scripts/generate_proto.sh` runs it with the other templates, and `./scripts/check_proto.sh`
fails on a stale `gen/`.

## Checks

```sh
./check.sh
```

runs `gofmt`, `go vet` and the tests, runs the concurrency tests again with the race detector, then
vets for each release target (Linux and macOS, amd64 and arm64). CI runs the same script as the `cli`
job. To run the whole store suite under the race detector, which takes several times longer:

```sh
go test -race ./internal/store/...
```

The store tests do not depend on time, threads or the network: the crash tests fail a step by its
number on an in-memory filesystem, so the same step fails on every run. A real directory is used
only to run the contract suite once on the operating system's filesystem.
