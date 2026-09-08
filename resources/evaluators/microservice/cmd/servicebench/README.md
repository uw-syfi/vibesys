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
- atomically writing the summary JSON and optionally writing raw NDJSON;
- reporting the workload's objective metric, and the latency percentiles it
  measured, on the evaluator record stream when `--vs-output` names one; and
- returning a nonzero status for invalid benchmark results.

`--mode benchmark` is the default. `--mode accuracy` uses the same resolved
targets, transport sessions, random seed handling, and atomic JSON output for
applications with a registered Go accuracy adapter. Hotel Reservation uses the
evaluator package's `hotel-correctness` Python entrypoint instead. Managed
candidate mode additionally proves that every readiness endpoint stops before
restarting after an OS-contained crash. Managed candidates require Bubblewrap;
the command fails closed when a dedicated PID namespace cannot be created.

It should contain orchestration only. Protocol behavior belongs in `drivers/`,
application behavior in `apps/`, and measurement behavior in `engine/`.

## Trace Graph

`servicebench trace` runs the normal managed benchmark path, then asks the
configured telemetry collector for a separate, versioned trace graph. It emits
a bounded box-and-arrow call graph and waterfall on stdout for human inspection
while preserving the full machine-readable graph on disk:

```bash
servicebench trace \
  --workload benchmark/workload.toml \
  --run-command-json '["./benchmark/run.sh"]' \
  --telemetry-command-json '["otelcapture","--input-json","metrics/otlp.ndjson"]' \
  --telemetry-output metrics/telemetry.json \
  --trace-graph-json metrics/trace-graph.json \
  --trace-graph-text metrics/trace-graph.txt \
  --output-json metrics/benchmark.json
```

The graph includes only complete traces observed in benchmark measurement
windows. The version 2 graph calculates a per-root critical path with the
`wall_clock_active_leaf_v1` algorithm. Span boundaries partition root wall
time, and each interval is attributed to the active synchronous leaf that
finishes latest. Overlapping siblings are therefore not double-counted, and
collapsed RPC client/server pairs retain their envelope time. Async and linked
relationships are excluded from the synchronous path and counted in the
graph's exclusion summary.

The JSON contains aggregate critical-path duration and node-contribution
distributions, plus a representative ordered path. `--trace-max-roots`,
`--trace-max-nodes`, and `--trace-timeline-width` bound the text view; the JSON
remains complete. The text reports every omission explicitly and marks the
representative critical-path segments. This command does not yet feed the
trace graph into the VibeSys optimization loop.
