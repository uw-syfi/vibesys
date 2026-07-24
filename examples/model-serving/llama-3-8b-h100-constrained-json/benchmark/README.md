# Constrained JSON Benchmark

Closed-loop streaming `/v1/completions` benchmark with concurrency 16 and
`guided_json` schema requests. `aggregate_throughput` counts only schema-valid
responses, and `p99_latency_ms` is computed only over schema-valid responses.
