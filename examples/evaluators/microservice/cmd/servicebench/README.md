# `servicebench` command

This package is the CLI entry point for benchmarking or accuracy-checking a
microservice application. It is the composition root that registers the
built-in HTTP driver and the separate benchmark and accuracy adapters before
invoking the selected shared runner.

The command is responsible for:

- workload, profile, target, load, independent schedule/fixture seed, and output
  flags;
- validating command-line overrides and registered extensions;
- hashing the fully resolved workload;
- signal-aware execution;
- atomically writing the summary JSON and optionally writing raw NDJSON; and
- returning a nonzero status for invalid benchmark results.

`--mode benchmark` is the default. `--mode accuracy` uses the same resolved
targets, transport sessions, random seed handling, and atomic JSON output but
runs the application's independent exhaustive accuracy adapter. Managed
candidate mode additionally proves that every readiness endpoint stops before
restarting after an OS-contained crash. Managed candidates require Bubblewrap;
the command fails closed when a dedicated PID namespace cannot be created.

It should contain orchestration only. Protocol behavior belongs in `drivers/`,
application behavior in `apps/`, and measurement behavior in `engine/`.
