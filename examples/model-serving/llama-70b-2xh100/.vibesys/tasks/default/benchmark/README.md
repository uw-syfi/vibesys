# Multi-GPU Chat/Completion Benchmark

Closed-loop streaming `/v1/completions` benchmark with concurrency 8,
medium-length synthetic prompts (~256 words), and 128-token outputs. Designed
for dense 70B models on multi-GPU nodes. The benchmark emits
`aggregate_throughput` and `p99_latency_ms` as top-level fields for Pareto
optimization.
