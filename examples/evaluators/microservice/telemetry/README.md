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

## Instrumentation boundary

The application must export spans with synchronized timestamps and meaningful
`service.name` resources. Automatic language-specific agent injection,
collector deployment, and backend querying are deployment concerns layered on
this contract. A scenario can configure those pieces through its managed run
command without changing the evaluator.

Configured telemetry fails closed when the collector exits unsuccessfully,
times out, writes malformed data, or reports no spans inside the measured
windows. Telemetry remains diagnostic; the benchmark objective and correctness
constraints determine whether a candidate improved.
