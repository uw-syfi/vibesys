# vibesys-evaluator

Build VibeSys benchmark evaluators for services: workloads, phases and validation.

```
pip install vibesys-evaluator
```

The Python package is a thin wrapper over a Go core. The wheels for Linux and macOS (x86_64 and
arm64) bundle the Go binaries (`vibesys-evaluator`, `loadgen` and `otelcapture`), so one
`pip install` is all a task needs: no Go toolchain, no `go install`. The Linux wheels are static
and carry both the manylinux and the musllinux tags.

The package looks for each binary in the wheel first, then in an environment variable
(`VIBESYS_EVALUATOR_BIN`, `VIBESYS_EVALUATOR_LOADGEN`, `VIBESYS_EVALUATOR_OTELCAPTURE`), then on
`PATH`. A source distribution, or a platform without a wheel, has no bundled binary: build the
binaries with Go and name them in those variables.

```
go install github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/cmd/vibesys-evaluator@<tag>
go install github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/cmd/loadgen@<tag>
export VIBESYS_EVALUATOR_BIN="$(command -v vibesys-evaluator)"
```

Go authors import the module `github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go` (tags
`sdk/vibesys-evaluator/go/v<semver>`, cut with each release) and need no wheel.

The design and the public API are in the repository: `sdk/vibesys-evaluator/docs/spec.md`.
