# Qwen3.6-35B-A3B Throughput Benchmark

Closed-loop streaming `/v1/completions` benchmark with concurrency 16,
medium-length synthetic prompts (~256 tokens), and 128-token outputs. Designed
for the from-scratch sparse-MoE Qwen3.6-35B-A3B server across 2 H100s. The
benchmark emits `aggregate_throughput` and `p99_latency_ms` as top-level fields
for Pareto optimization.

Run against a live server:

    python benchmark.py --url http://localhost:8000 --output-json result.json

Do not lower prompt length, duration, concurrency, or `max_tokens` to inflate
the score; the evaluator fixes these.
