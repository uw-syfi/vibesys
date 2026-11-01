# Evaluators

Open this when a task needs an accuracy or benchmark program beyond a short
task-local script, or when changing a shared evaluator package, the evaluator
result protocol, or its reader `vs-evaluator-protocol`.

## Task-local or packaged

- **Task-local**: the commands live in the task directory and the manifest
  names them with `command = [...]`. Use this for checks and workloads that
  only one task needs. `[evaluator] source = "<dir>"` copies a task-local
  evaluator tree into the workspace under `_evaluator/`.
- **Packaged**: a directory in the bundled evaluator collection with a
  `vibesys.evaluator.toml`, requested by exact `[evaluator] name` and
  `version`; task commands then use `entrypoint` and `args`. Use this when
  several tasks share the evaluator. The runtime records the package's
  `sha256:` content digest with the run.

The bundled collection is staged into the wheel, so packaged evaluators reach
installed users (`test_evaluator_packages_dir_points_at_the_resources_tree`).
The resources README may still say otherwise; trust the packaging test.

## The package manifest

The evaluator package metadata model in `vs-runtime` is the schema. Read an
existing small package for a real example, and the evaluator collection's
README for the tokens.

- `entrypoints` maps a public name to an argv prefix. Task `args` are appended
  without a shell. `${PACKAGE_ROOT}` expands to the package directory;
  `${PROJECT_ROOT}` in a task argument expands to the candidate root in the
  run environment.
- `toolchains` asks the run environment for Go or Rust.
- `[tools.<name>]` with `kind = "cargo-git"` installs a Rust binary from a
  full commit SHA with `--locked`; reference it as one whole argv element
  `${TOOL:<name>/<bin>}`. List the Debian packages its build needs in
  `system_packages`; a Docker target installs exactly those.
- `protocol_version` in this file is the package-metadata schema version, not
  the result protocol version the benchmark speaks.

The bundled collection is loaded as a whole when any task resolves a package,
and a duplicate `name` plus `version` is an error, so bump `version` when you
fork a package for an incompatible change.

## The result protocol

The protocol document shipped with the evaluator SDK is the authoritative
definition. A benchmark with `result_protocol = 2` receives `--vs-output <path>`
and writes JSON lines there: one `hello` declaring its metrics, then one
`result` row or an `error`. The run's objectives must name metrics the
evaluator declares as required
(`test_check_objectives_names_the_missing_objective_and_the_declared_metrics`).
Conformance fixtures hold the reader and the Go SDK to the same corpus
(`test_every_fixture_has_an_expectation`); add a fixture when you change the
protocol.

## Whose fault a failure is

A failed benchmark is the candidate's, the machinery's (infrastructure,
measured again within the submission bound), or ambiguous (measured once more,
then charged to the candidate). One table in `vs-runtime` decides it from how
the stage ended and what the evaluator wrote
(`test_every_signal_and_record_has_a_class`). What an evaluator author controls:

- **Write `hello` first, before any long setup.** A nonzero exit after
  `hello` is the candidate's; the same exit with no record at all is only
  ambiguous, so a broken candidate costs an extra measurement.
- **Report a candidate failure as an `error` record**, with what was measured
  before it stopped, rather than exiting without a record.
- **Do not catch and relabel infrastructure faults.** A lost node or a
  timeout in the run environment has no exit status and is classified as
  infrastructure without the evaluator's help.
- A candidate that edits evaluator-owned files fails as its own fault.
- Log patterns that prove a serving candidate died by its own fault at startup
  belong to the domain's startup-cause declaration, not to a task.

## Go SDK and module versions

Go evaluators require the published `vseval` module by version, never through
a local `replace`, because the package runs from a copy where a relative path
does not resolve (`test_evaluator_module_has_no_local_replace`). To change the
SDK and a consumer together, use an uncommitted `go.work`, then tag the SDK and
bump each consumer's `go.mod`. Check the version in the consumers' `go.mod`
files; the SDK README may name an older one.

## Benchmark design

- Rendezvous all workers before the measurement window opens, and complete at
  least one full workload batch per worker, so scheduler delay cannot produce
  an empty or partial-mix sample.
- Gate the benchmark on accuracy unless the measurement is meaningful without
  it (`requires_accuracy`, default true).
- For serving tasks, declare a short fixed `[profile]` workload that skips the
  benchmark's preflight and warmup gates, so profiles exist below the gate
  ([running-vibesys](../../../../docs/running-vibesys.md)).
