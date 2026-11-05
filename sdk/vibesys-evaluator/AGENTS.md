# vibesys-evaluator: agent instructions

The repository's root [`AGENTS.md`](../../AGENTS.md) applies here too. This
package is what task writers build benchmark evaluators on: the Python
package `vibesys_evaluator`, loadgen (the Go binary in `loadgen/` that sends the requests),
the schema in `proto/vibesys/evaluator/v1/`, and the image
`vibesys-evaluator`. The design is [`docs/spec.md`](docs/spec.md).

## Public API rule

The public API is everything listed in [`api/public-api.txt`](api/public-api.txt)
(the names in each public module's `__all__`, their signatures and the
behaviour documented for them) and the schema in
`proto/vibesys/evaluator/v1/*.proto`, including the loadgen command line it
describes. Task writers depend on both through a pinned image.

- **A breaking change needs the user's approval first.** Stop and ask before
  making one, naming each break and the migration a task writer follows.
  Breaking means: a removed or renamed name; an incompatible signature (a
  parameter removed, renamed, reordered, made required, or a positional one
  added); a narrowed parameter or widened return type; a changed default; a
  change of behaviour that tasks rely on (including any change to a `Rand`
  stream, the visibility rule of `Versions`, or how latency and throughput
  are computed); and any change to the `.proto` files that `buf breaking`
  reports or that changes a field's meaning. Enforced in CI by `griffe check`
  against the last release and `buf breaking`; the approval itself is this
  rule, since no check can ask the user.
- **An additive change needs no approval, but must be used by an example**
  under `examples/` in the same PR. Every public name is used by at least
  one example. Unenforced until the examples run in CI (work package 6); a
  reviewer checks it.
- **Never hand-edit the snapshot.** `api/public-api.txt` is generated
  (griffe) from the code; CI regenerates it and fails on a difference.
  Regenerate it and review the diff.

## Working rules

- Keep the public surface minimal: inline type annotations and `py.typed`;
  an explicit `__all__` in each public module; everything else in
  underscore-private modules; keyword-only arguments with good defaults; one
  way to do each thing; plain data in and out (functions, dicts, frozen
  dataclasses); errors that say what to fix, naming the operation, key or
  file. `pyright --verifytypes vibesys_evaluator` passes. No hand-written
  `.pyi` files and no CODEOWNERS.
- Nothing runs in Python during a measured window, and loadgen never draws
  randomness that changes what is sent (only Poisson arrival times from the
  seed it is given). A feature that would need either goes into the plan
  that Python writes before the phase, or is designed with the user first.
- loadgen knows no task, operation semantics or model: everything it sends
  is in the requests file. It is an implementation behind the command line
  and the files, so another (a Rust build) can replace it; a feature is
  defined in the `.proto` and the spec first, then implemented.
- The `.proto` is the single source of truth for the files; the Python and Go
  types are generated from it and validated with protovalidate. Conformance
  fixtures in `fixtures/` are read and written by both sides' tests.
- One version for the Python package, the Go module tag
  (`sdk/vibesys-evaluator/loadgen/vX.Y.Z`) and the image.
- Task-specific code (an application's operations, dataset, manifest) lives
  in the task or under `examples/`, never in the package.
- Examples use generic names only: no private hostnames, paths or keys.
