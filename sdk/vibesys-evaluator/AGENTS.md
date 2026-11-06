# vibesys-evaluator: agent instructions

The repository's root [`AGENTS.md`](../../AGENTS.md) applies here too. This
package is what task writers build benchmark evaluators on: the Python
package `vibesys_evaluator` (a domain-free core and the domains
`microservices` and `llm_serving`), the Go SDK `go/workload` (request
generators in Go, for the microservices domain), the Go core `go/evaluator`
(sample lifecycle, records, histograms; `evaluator.Main` is the Go `main`) with
its command line `vibesys-evaluator <group> <verb>` that the Python package
wraps, loadgen (the Go binary in `go/cmd/loadgen` that sends the microservices
requests), the schema in
`proto/vibesys/evaluator/v1/`, and one image per domain,
`vibesys-evaluator-microservices` and `vibesys-evaluator-llm-serving`. The
design is [`docs/spec.md`](docs/spec.md).

## Public API rule

The public API is everything listed in [`api/public-api.txt`](api/public-api.txt)
(the names in each public module's `__all__`, their signatures and the
behaviour documented for them), the Go SDK listed in
[`api/go-workload.txt`](api/go-workload.txt) (`go doc -all` of
`go/workload`: its exported names, signatures and documented behaviour),
and the schema in `proto/vibesys/evaluator/v1/*.proto`, including the loadgen
and generator command lines it describes. Task writers depend on them through
a pinned image and a pinned Go module version.

- **A breaking change needs the user's approval first.** Stop and ask before
  making one, naming each break and the migration a task writer follows.
  Breaking means: a removed or renamed name; an incompatible signature (a
  parameter removed, renamed, reordered, made required, or a positional one
  added); a narrowed parameter or widened return type; a changed default; a
  change of behaviour that tasks rely on (including any change to a `Rand`
  stream, the visibility rule of `Versions`, or how latency and throughput
  are computed); for the Go SDK the same, Go's way (an exported name removed
  or renamed, a changed function signature, a struct field removed, a type
  parameter added, a changed default such as `Operation.Sample` 0); a change
  of the request stream a workload writes (Python and Go must keep writing
  the same one); and any change to the `.proto` files that `buf breaking`
  reports or that changes a field's meaning. Enforced in CI by `griffe check`
  against the last release, by `scripts/check_vibesys_evaluator_api.py`
  comparing `api/go-workload.txt` with the last release's, and `buf
  breaking`; the approval itself is this rule, since no check can ask the
  user. Record an approved Python break in `api/approved-breaks.toml`
  (`against` the last release tag, `approved_by`, `reason`); the API check
  accepts breaks against that release only.
- **An additive change needs no approval, but must be used by an example**
  under `examples/` in the same PR. Every public name is used by at least
  one example. Unenforced until the examples run in CI (work package 6); a
  reviewer checks it.
- **Never hand-edit the snapshots.** `api/public-api.txt` is generated
  (griffe) from the code and `api/go-workload.txt` by `go doc -all`; CI
  regenerates both and fails on a difference (the Python script and the Go
  test `TestAPISnapshotIsCurrent`). Regenerate them (`--write`;
  `go test ./workload -run APISnapshot -update`) and review the diff.

## Working rules

- Keep the public surface minimal: inline type annotations and `py.typed`;
  an explicit `__all__` in each public module; everything else in
  underscore-private modules; keyword-only arguments with good defaults; one
  way to do each thing; plain data in and out (functions, dicts, frozen
  dataclasses); errors that say what to fix, naming the operation, key or
  file. `pyright --verifytypes vibesys_evaluator` passes. No hand-written
  `.pyi` files and no CODEOWNERS.
- loadgen never draws randomness that changes what is sent (only Poisson
  arrival times from the seed it is given), and the request stream is a
  function of the PhaseInput alone: request `i` depends on the seed and the
  requests before it, never on timing or on responses (multi-step templates
  are filled by loadgen). The generator runs during the window, on the phase
  pod's CPUs; a feature that would make it depend on the measurement is
  designed with the user first.
- loadgen knows no task, operation semantics or model: everything it sends
  is in the request stream. It is an implementation behind the command line
  and the files, so another (a Rust build) can replace it; a feature is
  defined in the `.proto` and the spec first, then implemented.
- The `.proto` is the single source of truth for the files and the stream; the
  Python and Go types are generated from it and validated with protovalidate.
  Conformance fixtures in `fixtures/` are read and written by both sides'
  tests; `fixtures/rand.json` pins `Rand` for both SDKs.
- The two front ends stay twins: a concept added to one is added to the other
  (or recorded as one-sided in `docs/spec.md`), and the kv-store example's
  Python and Go generators must keep writing the same stream (the end-to-end
  test checks it).
- One version for the Python package, the Go module tag
  (`sdk/vibesys-evaluator/go/vX.Y.Z`) and the images.
- The core (`vibesys_evaluator` outside a domain package) imports no domain,
  and a domain imports the core and itself only; mechanisms in the core never
  name a domain. Enforced by `python/tests/test_layering.py`.
- A harness a domain wraps (such as `agentx-harness`) is pinned exactly in that
  domain's image lock (`image/<domain>/`), and the domain refuses to run under
  any other installed version.
- Task-specific code (an application's operations, dataset, manifest) lives
  in the task or under `examples/`, never in the package.
- Examples use generic names only: no private hostnames, paths or keys.
