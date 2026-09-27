# Qwen3.6-35B-A3B Request Factory Benchmark

Request Factory replays 64 independent streamed `/v1/completions` requests
under saturated load, with maximum concurrency 16. Each prompt has 256 tokens
and requests up to 128 output tokens. The workload targets the from-scratch
sparse-MoE Qwen3.6-35B-A3B server across 2 H100s.

Exact token lengths replace the old approximate word count, and the fixed
trace replaces the legacy 30-second duration window. RF counts completion
token IDs rather than nonempty SSE chunks and reports p90 rather than p99
latency. These methodology changes are intentional; old and new scores are
not directly comparable. The evaluator entrypoint validates the RF summary and
maps the metrics to VibeSys result protocol 2.

The tokenizer is pinned to Hugging Face revision
`995ad96eacd98c81ed38be0c5b274b04031597b0`; the evaluator resolves that exact
cached snapshot or downloads that exact `tokenizer.json` before RF starts. The
entrypoint generates its deterministic synthetic text corpus in a temporary
directory and sets an explicit token-pool limit from prompt length and request
count. It fails if RF reports that the pool is too short for a prompt.

The evaluator injects the trusted RF engine path. For CPU-only request-path
validation, run `uv run python -m tests.examples.request_factory_cpu_smoke
--profile examples/model-serving/qwen3.6-35b-a3b-2xh100/benchmark/cpu_smoke.toml
--request-factory-engine <RF_ENGINE>`. The fake validates request shape,
protocol-v2 output, and HTTP, malformed/truncated SSE, and output-mismatch
failures. Fake-server metrics are not serving-performance results.
