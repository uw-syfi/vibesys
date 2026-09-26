# DeepSeek-V3.2 Request Factory benchmark

Request Factory drives the OpenAI-compatible `/v1/completions` endpoint using
256 independent requests at saturation, concurrency 64, 8192-token synthetic
prompts, and 1024-token output targets. The evaluator entrypoint generates a
deterministic synthetic corpus in a temporary directory and explicitly sizes the token pool
to at least twice the prompt length or the request count, whichever is larger.
It fails if RF reports an undersized pool.

The tokenizer is pinned to Hugging Face revision
`a7e62ac04ecb2c0a54d736dc46601c5606cf10a6`; the evaluator resolves that exact
cached snapshot or downloads that exact `tokenizer.json` before RF starts.

The VibeSys protocol-v2 objectives are `output_token_throughput_per_s` and
`p90_latency_ms`. RF counts completion token IDs and reports end-to-end latency,
instead of counting nonempty SSE chunks and reporting p99 latency as the legacy
driver did. The fixed trace replaces its 120-second rolling window. These
methodology changes are intentional; scores are not directly comparable with
legacy benchmark results. RF failure, incomplete-step, and output-length
mismatch counts must all be zero or the benchmark fails.

For CPU-only request-path validation, run
`uv run python -m tests.examples.request_factory_cpu_smoke
--profile examples/model-serving/deepseek-v3.2-8xb200/benchmark/cpu_smoke.toml
--request-factory-engine /path/to/session_runner`. The shared harness uses a
strict local fake completions server and a tiny local tokenizer. It validates
prompt uniqueness, protocol-v2 output, and HTTP, malformed/truncated SSE, and
output-mismatch failures. Fake-server throughput is not a serving-performance
result.
