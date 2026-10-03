## Microservice profile capture

Profile the same network path that the benchmark exercises. Start from the
objective and manifest to identify the base URL, gateway, service ports, and
benchmark command.

Useful evidence includes:

- Benchmark output with headline metric, latency percentiles, throughput,
  success rate, and error rate.
- Gateway/proxy logs and upstream timing fields.
- Service logs showing retries, timeouts, connection churn, queueing, or slow
  dependency calls.
- Container or process CPU and memory during steady-state load.
- Database/cache stats such as hit rate, slow queries, connection counts, and
  pool saturation.
- A servicebench `telemetry` report with in-window service, span, and datastore
  mean, p50, p95, p99, maximum, count, and error count.
- A servicebench schema-v2 trace graph with workload-correlated synchronous
  critical-path contribution and a representative path decomposition.

When using a system profiler, capture a steady-state run after warmup. For
Docker Compose deployments, record the service or container being profiled and
the benchmark load level used. Do not treat a single internal span or local
handler timing as the final metric unless the objective defines it that way.
Use normalized OpenTelemetry rows to rank likely bottlenecks and compare the
same span across candidates. Critical-path contribution is wall-clock
attribution: overlapping sibling calls are not additive, and linked or async
relationships are excluded from the synchronous path. Report those exclusions
as a coverage limit. Keep the benchmark's `primary_value` as the authoritative
score, and reject telemetry or trace graphs that are empty or do not match the
workload identity and evaluator measurement windows.
