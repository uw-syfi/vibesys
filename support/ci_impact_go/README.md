# Go CI impact prototype

This is an independent Go implementation of [`support/ci_impact`](../ci_impact/README.md) for evaluating Go as the language for repository tooling. It reads the same top-level [`ci-components.toml`](../../ci-components.toml); it does not shell out to Python. The current CI workflow uses the Python implementation. The Go prototype has its own conditional test job.

Run from the repository root:

```sh
./support/ci_impact_go/ci-impact-go explain src/server/api/schema.py
./support/ci_impact_go/ci-impact-go explain src/server/api/schema.py --json
./support/ci_impact_go/ci-impact-go plan --base main --head HEAD --event pull_request --json
./support/ci_impact_go/ci-impact-go validate
(cd support/ci_impact_go && go test ./...)
```

`plan` accepts `--github-output PATH` and appends job booleans plus JSON arrays for `native_targets`, `native_languages`, and `pnpm_packages`. `native_languages` lists the distinct `go` and `rust` toolchains needed for the selected native roots. The CLI exits with status 2 for invalid policy, unknown changed paths, or Git failures. The wrapper invokes `go run`, so it downloads the one TOML dependency on first use. `go build .` produces a standalone executable for repeated use. Set `CI_IMPACT_ROOT` when running that executable outside the repository tree.

The tool reads Tach modules, pnpm workspace package manifests, and native Cargo/Go manifest roots. It adds the cross-component edges from the top-level policy and walks reverse dependencies. Git diff handling includes both names of renames, merge bases for pull requests, exact push and merge-group endpoints, and initial pushes. No Python code is imported or executed.
