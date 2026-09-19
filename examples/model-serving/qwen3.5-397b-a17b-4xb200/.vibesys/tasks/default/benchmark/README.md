# Qwen3.5-397B-A17B Throughput Benchmark

Closed-loop streaming `/v1/completions` benchmark with concurrency 48,
long synthetic prompts (~2048 tokens), and 512-token outputs. Designed for
the from-scratch hybrid Gated-DeltaNet/MoE Qwen3.5-397B-A17B server across
4 B200s. The benchmark emits `aggregate_throughput` and `p99_latency_ms` as
top-level fields for Pareto optimization.

Run against a live server:

    python benchmark.py --url http://localhost:8000 --output-json result.json

Do not lower prompt length, duration, concurrency, or `max_tokens` to inflate
the score; the evaluator fixes these.
