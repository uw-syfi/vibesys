# vibesys-cli

`vibesys-cli` is a standalone Go binary. The VibeSys optimization skills call it for anything that
must be deterministic: measuring a commit, comparing results, promoting a winner, and the
lifecycle of the jobs that do that work. The skills hold judgment (what to try, when to stop); the
CLI holds determinism. It does not launch an agent.

The Python package installs a different command, `vibesys`, and `vibesys compare` means something
else there. The Go binary keeps the name `vibesys-cli`.

Work is landing in stages. The `cmd/vibesys-cli` entrypoint prints its version and nothing else
yet. The module so far is the foundation later pieces build on: the layout, the layer rules, the
contract, the domain types and the interfaces. Rows marked "planned" below do not exist yet.

## Layout

| Path | Role |
| --- | --- |
| `cmd/vibesys-cli/` | Argument parsing and output. Nothing else. |
| `internal/core/` | Pure domain types, and later the pure rules (keys, verdicts, promotion checks): no I/O, no clock, no goroutines. |
| `internal/store/` | `Store`: the logical interface to results, events, logs, job state and plans. No paths, no formats. |
| `internal/store/filestore/` | The only code that knows the on-disk layout. Owns migrations. Planned: the package holds only its doc comment so far. |
| `internal/store/memstore/` | Planned: in-memory `Store` for tests. Passes the same contract suite as `filestore`. |
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

runs `gofmt`, `go vet` and the tests, then vets for each release target (Linux and macOS, amd64 and
arm64). CI runs the same script as the `cli` job.
