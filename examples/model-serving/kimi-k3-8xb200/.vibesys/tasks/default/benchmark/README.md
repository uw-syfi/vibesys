# Kimi-K3 Throughput Benchmark

Closed-loop streaming `/v1/completions` benchmark with concurrency 32,
long synthetic prompts (~4096 tokens), and 2048-token outputs over a 120
second window. Designed for the from-scratch frontier-scale MoE Kimi-K3
server across 8xB200. The benchmark emits `aggregate_throughput` and
`p99_latency_ms` as top-level fields for Pareto optimization.

Run against a live server:

    python benchmark.py --url http://localhost:8000 --output-json result.json

Do not lower prompt length, duration, concurrency, or `max_tokens` to inflate
the score; the evaluator fixes these.
