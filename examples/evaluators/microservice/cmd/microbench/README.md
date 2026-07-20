# `microbench` command

This package is the CLI entry point for running a microservice workload. It is
the composition root that registers the built-in HTTP driver and application
adapters before invoking the shared engine.

The command is responsible for:

- workload, profile, target, load, seed, and output flags;
- validating command-line overrides and registered extensions;
- hashing the fully resolved workload;
- signal-aware execution;
- atomically writing the summary JSON and optionally writing raw NDJSON; and
- returning a nonzero status for invalid benchmark results.

It should contain orchestration only. Protocol behavior belongs in `drivers/`,
application behavior in `apps/`, and measurement behavior in `engine/`.
