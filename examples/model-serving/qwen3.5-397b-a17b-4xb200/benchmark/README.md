# Qwen3.5-397B-A17B Request Factory Benchmark

Request Factory replays 64 independent streamed `/v1/completions` requests
under saturated load, with maximum concurrency 48. Each prompt has 2048 tokens
and requests up to 512 output tokens. The workload targets the from-scratch
hybrid Gated-DeltaNet/MoE Qwen3.5-397B-A17B server across 4 B200s.

Exact token lengths replace the old approximate word count, and the fixed
trace replaces the legacy 90-second duration window. RF counts completion
token IDs rather than nonempty SSE chunks and reports p90 rather than p99
latency. These methodology changes are intentional; old and new scores are
not directly comparable. The evaluator entrypoint validates the RF summary and
maps the metrics to VibeSys result protocol 2.

The tokenizer is pinned to Hugging Face revision
`8472618112abcbd45acbcdc58436aff4233c23f7`; the evaluator resolves that exact
cached snapshot or downloads that exact `tokenizer.json` before RF starts. The
evaluator injects the trusted RF engine path. For CPU-only request-path
validation, run `uv run python -m tests.examples.request_factory_cpu_smoke
--profile examples/model-serving/qwen3.5-397b-a17b-4xb200/benchmark/cpu_smoke.toml
--request-factory-engine <RF_ENGINE>`. The entrypoint generates its deterministic text corpus in
the per-run temporary directory and sets the token-pool limit to the larger of
twice the prompt length or the request count. It fails if RF warns that the
pool is shorter than a prompt. The fake also validates protocol-v2 output and
HTTP, malformed/truncated SSE, and output-mismatch failures. Fake-server
metrics are not serving-performance results.
