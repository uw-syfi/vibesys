# Telemetry Contract

This package owns the optional diagnostic-telemetry boundary for the reusable
microservice evaluator. It correlates traces to benchmark measurement windows,
normalizes internal latency evidence, and validates the artifact attached to a
servicebench result.

## Collector protocol

`servicebench --telemetry-command-json '["collector", ...]'` executes the
trusted command after measured trials and appends:

```text
--request-json <temporary-request> --output-json <configured-output>
```

The request contains `schema_version`, `workload_name`, `workload_hash`, and
the exact UTC `measurement_windows`. The command must atomically produce a JSON
report with `schema_version`, source and collection metadata, span/error
counts, and latency rows grouped as `services_by_p95`, `spans_by_p95`, and
optionally `datastores_by_p95`. Every row contains count, error count, mean,
p50, p95, p99, and maximum latency in milliseconds.

Collectors may use any backend or language. This directory includes
`cmd/otelcapture`, a generic normalizer for OTLP JSON and newline-delimited OTLP
JSON. It recognizes standard OpenTelemetry resource/span structures, obtains
service identity from `service.name`, and recognizes datastore spans through
`db.*` semantic attributes. This keeps application and protocol knowledge out
of the evaluator.

## Trace graph artifact

When a collector receives `--trace-graph-json`, `cmd/otelcapture` also
reconstructs workload-observed traces into trace graph schema version 2. The
artifact groups traces by root service and semantic operation, then records:

- path-aware service/operation nodes and synchronous, asynchronous, and linked
  edges;
- count, errors, mean, p50, p95, p99, and maximum inclusive and exclusive
  duration for every node;
- captured, eligible, and excluded trace counts, including exclusion reasons
  and per-trial evidence;
- unambiguous client/server pair collapsing with unmatched-span counters; and
- one deterministic representative successful trace nearest the root p95 for
  a waterfall view; and
- a per-root critical-path summary using the
  `wall_clock_active_leaf_v1` algorithm.

Operation identity uses safe OTel semantic attributes such as `http.route`,
`rpc.service`, `rpc.method`, `db.system.name`, and `db.operation.name`, then
falls back to the span name. Arbitrary attributes, request bodies, raw query
strings, and credentials are not copied into the artifact.

Exclusive duration means a span's wall-clock interval minus the union of its
direct-child intervals. It is useful attribution evidence, but it is not CPU
time. Traces with missing parents, duplicate IDs, multiple roots, cycles,
window crossings, or inconsistent parent/child clocks are excluded rather
than partially graphed. A graph request fails when no eligible trace remains.

### Critical-path contract

Critical-path analysis is computed independently for each eligible trace before
traces are grouped into root graphs. The result covers the root request's
wall-clock interval exactly. Span boundaries partition that interval. Each
partition is attributed to the active synchronous leaf that finishes latest,
then by depth and stable span identity for deterministic ties. Parent-only
partitions remain attributable to the parent. Sequential sibling calls
therefore contribute their separate intervals, while overlapping siblings are
not double-counted.

Unambiguous client/server RPC pairs retain the client interval as the RPC
envelope, so transport time remains represented when the pair is collapsed into
one graph node. Asynchronous producer/consumer relationships and span links are
not treated as ordinary synchronous children. They are excluded from the
synchronous path and counted in `async_relationships_excluded`. Work outside
the request root is consequently not attributed to that request's critical
path.

Each root contains the critical-path duration distribution, aggregate
`nodes_by_contribution` distributions, and a representative path with ordered
node segments and offsets. Aggregate distributions include count, mean, p50,
p95, p99, and maximum values. The JSON artifact is the authoritative machine
contract; the text renderer presents the representative path and marks its
critical segments for inspection.

`--trace-graph-text` optionally writes the deterministic human view: boxes and
arrows show call structure, and a separate timeline shows span overlap.
Rendering limits affect only this text view and report explicit omitted counts.
The versioned JSON is the machine contract. Automatic consumption by the
VibeSys optimization loop remains a follow-up integration; this evaluator
currently produces and validates the diagnostic artifact only.

## Instrumentation boundary

The application must export spans with synchronized timestamps and meaningful
`service.name` resources. Automatic language-specific agent injection,
collector deployment, and backend querying are deployment concerns layered on
this contract. A scenario can configure those pieces through its managed run
command without changing the evaluator.

## Injection harness

`cmd/otelinject` (package `telemetry/inject`) is the evaluator-owned
implementation of that deployment layer for Docker Compose scenarios. A
scenario declares its services in a small `telemetry.toml`:

```toml
version = 1
collector_service_name = "jaeger"  # default "otel-collector"
sample_ratio = 0.1                 # in (0, 1], default 0.1

[services]
frontend = "jaeger-native"
api = "java"      # requires java_agent_path at the top level
worker = "python"
web = "node"
```

At container start the scenario's run command invokes:

```text
otelinject --compose <candidate compose> --config <telemetry.toml> \
  --metrics-dir <host capture dir> --output <override path>
```

which reads the candidate's *current* compose file and emits a compose
override adding one pinned `opentelemetry-collector-contrib` service plus
per-service environment/volume fragments selected by runtime group:

- `jaeger-native` — apps with built-in Jaeger tracing; the collector
  impersonates the configured collector service name (for DeathStarBench,
  `jaeger`) and only `JAEGER_SAMPLE_RATIO` is set.
- `java` — mounts the configured OpenTelemetry javaagent and sets
  `JAVA_TOOL_OPTIONS` plus standard `OTEL_*` exporter/sampler variables.
- `python` — sets standard `OTEL_*` variables; the image must bundle
  `opentelemetry-distro`.
- `node` — sets `NODE_OPTIONS` to require
  `@opentelemetry/auto-instrumentations-node/register` plus `OTEL_*`.

Unknown runtime names and unknown config keys are hard errors. Configured
services missing from the compose file are skipped with a warning so candidate
refactors cannot break deployment; if none remain, injection fails. The
collector listens for OTLP, Jaeger, and Zipkin traffic and appends spans as
OTLP NDJSON to a bind-mounted file under the metrics dir, which
`cmd/otelcapture --settle-seconds 5` normalizes after the measured trials.

Configured telemetry fails closed when the collector exits unsuccessfully,
times out, writes malformed data, or reports no spans inside the measured
windows. Telemetry remains diagnostic; the benchmark objective and correctness
constraints determine whether a candidate improved.
