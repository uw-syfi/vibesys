# SDK

Reusable packages for authors of VibeSys input bundles.

Unlike `libs/`, which houses framework-internal packages imported by VibeSys
core, packages here are consumed by user-authored code that runs inside
VibeSys-managed environments (benchmarks, accuracy checkers, reference
generators). The VibeSys framework itself does not import these packages; it
only observes their output (JSON results, exit codes).

Each package is independently installable with minimal dependencies so that
benchmark and checker scripts can run in lean, purpose-built environments
without pulling in the full VibeSys dependency tree.

## Packages

| Package | Purpose |
|---------|---------|
| `vs-bench` | Benchmark toolkit: SSE transport, load scheduling, statistics |
| `vs-evaluator` | Evaluator result protocol: spec, conformance fixtures, Go SDK |
| `vibesys-evaluator` | Benchmark evaluators for services: Python API, loadgen, deploy layer (spec only so far: `docs/spec.md`) |

## Co-developing an SDK and a consumer

Consumers depend on published versions, never on a relative `replace`. The queue
evaluator, for example, requires
`github.com/uw-syfi/vibesys/sdk/vs-evaluator/vseval v0.1.0` and resolves it from
the module proxy. This is not incidental: VibeSys copies an evaluator package to
`_evaluator/<name>` in the candidate workspace before running it, where a
relative path no longer resolves. Requiring the published version means CI
builds the same dependency a run does.

To change an SDK and a consumer together, create a Go workspace at the
repository root:

```bash
go work init ./sdk/vs-evaluator/vseval ./resources/evaluators/queue
```

`go.work` and `go.work.sum` are gitignored and must stay that way. A committed
workspace would redirect CI to the local SDK source while real runs resolve the
published version, so CI could pass against code no run executes.

An SDK change reaches consumers only once it is tagged (Go module tags for a
subdirectory carry the full path, `sdk/vs-evaluator/vseval/vX.Y.Z`) and each
consumer's `go.mod` requires the new version.
