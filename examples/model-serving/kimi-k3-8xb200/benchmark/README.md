# Kimi-K3 Request Factory benchmark

Request Factory drives the OpenAI-compatible /v1/completions endpoint using
256 independent requests at saturation, concurrency 32, 4096-token
synthetic prompts, and 2048-token output targets. The adapter generates a
deterministic synthetic corpus in a temporary directory and explicitly sizes
the token pool to at least twice the prompt length or the request count,
whichever is larger. It fails if RF reports an undersized pool.

Kimi-K3 does not publish a `tokenizer.json`, which is the format RF consumes.
The adapter resolves `tiktoken.model` at immutable model revision
`f831ab66814297da540d832a5235f8e904f29d06`, converts it in a temporary
directory with Kimi's exact split pattern and token IDs, and gives that local
file to RF. `--tokenizer` may instead name an existing compatible
`tokenizer.json`, including for an offline run.

The VibeSys protocol-v2 objectives are output_token_throughput_per_s and
p90_latency_ms. RF counts completion token IDs and reports end-to-end latency,
instead of counting nonempty SSE chunks and reporting p99 latency as the legacy
driver did. The fixed trace replaces its 120-second rolling window. These
methodology changes are intentional; scores are not directly comparable with
legacy benchmark results. RF failure, incomplete-step, and output-length
mismatch counts must all be zero or the benchmark fails.

For CPU-only request-path validation, run
`uv run python -m tests.examples.request_factory_cpu_smoke --profile examples/model-serving/kimi-k3-8xb200/benchmark/cpu_smoke.toml --request-factory-engine <RF_ENGINE>`.
Fake-server throughput is not a serving-performance result.
