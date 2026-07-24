# High-Concurrency Benchmark

Closed-loop streaming `/v1/completions` benchmark with concurrency 64, short
synthetic prompts, and short 16-token outputs. The benchmark emits
`aggregate_throughput` and `p99_latency_ms` as top-level fields for Pareto
optimization.
