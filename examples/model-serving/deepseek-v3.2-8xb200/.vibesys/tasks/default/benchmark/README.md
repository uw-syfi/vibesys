# DeepSeek-V3.2 Throughput Benchmark

Closed-loop streaming `/v1/completions` benchmark with concurrency 64,
long synthetic prompts (~8192 tokens, distinct per request rather than a
shared prefix), and 1024-token outputs. Designed for the from-scratch FP8 MoE
DeepSeek-V3.2 (MLA + DeepSeek Sparse Attention) server across 8xB200. The
benchmark emits `aggregate_throughput` and `p99_latency_ms` as top-level
fields for Pareto optimization.

Run against a live server:

    python benchmark.py --url http://localhost:8000 --output-json result.json

Do not lower prompt length, duration, concurrency, or `max_tokens` to inflate
the score, and do not collapse the distinct per-request prompts into a single
cached prefix; the evaluator fixes these.
