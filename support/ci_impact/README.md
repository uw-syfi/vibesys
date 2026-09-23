# CI impact selector

This utility is a repository-agnostic change selector. It reads a TOML policy, discovers components through configured manifest adapters, maps changed paths to components, and walks reverse dependency edges to select jobs. Component IDs are opaque to the graph walker. Adapters translate manifest formats into the common component model.

Run it from a repository root with a `ci-impact.toml` policy:

```sh
./support/ci_impact/ci-impact plan --base <base-ref> --head HEAD
./support/ci_impact/ci-impact explain path/to/file --json
./support/ci_impact/ci-impact validate
./support/ci_impact/ci-impact --config path/to/policy.toml validate
(cd support/ci_impact && go test ./... && go vet ./...)
```

`--config` accepts a path relative to the repository root or an absolute path. The directory containing an absolute config file is treated as the repository root. `CI_IMPACT_ROOT` can explicitly set the root when needed. Configured jobs and collection names become GitHub outputs, so a workflow can keep stable output names while the selector remains generic.

The built-in discovery adapters are `tach`, `package_json`, and `manifest_directories`. Each adapter parses one manifest format and returns common component records. Paths, ID prefixes, source classes, ownership groups, job assignments, manifest globs, language mappings, and selected native roots come from the policy. The graph walker knows no language-specific ID prefixes.

To add a manifest format, implement `discoveryAdapter` in a new `discover_*.go` file, register it in `discoveryAdapters`, and add its adapter-specific fields to the policy schema. Add any language command arrays under `native_checks.commands`. The graph walker and selector remain unchanged.

Native checks are configured as argument arrays by language, with a timeout and optional per-root environment overrides or line assertions. The utility does not infer commands from the repository or shell-evaluate config values.

The policy format is intentionally small. Components may declare roots, exact files, jobs, classes, and dependency IDs. `[[edges]]` declares cross-component effects. `[[collections]]` selects output values by component class and field. Unknown config keys, missing dependencies, cycles, unknown paths, unsafe paths, unregistered in-scope manifests, and conflicting manifests fail validation.
