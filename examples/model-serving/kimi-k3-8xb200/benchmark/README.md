# Kimi-K3 Request Factory benchmark

Request Factory drives the OpenAI-compatible /v1/completions endpoint using
256 independent requests at saturation, concurrency 32, 4096-token
synthetic prompts, and 2048-token output targets. The adapter materializes a
deterministic synthetic corpus with a 65,536-token pool, writes the RF trace,
and passes it to the pinned VibeSys RF evaluator.

The VibeSys protocol-v2 objectives are output_token_throughput_per_s and
p90_latency_ms. RF counts completion token IDs and reports end-to-end latency,
instead of counting nonempty SSE chunks and reporting p99 latency as the legacy
driver did. The fixed trace replaces its 120-second rolling window. These
methodology changes are intentional; scores are not directly comparable with
legacy benchmark results. RF failure, incomplete-step, and output-length
mismatch counts must all be zero or the benchmark fails.

For CPU-only request-path validation, run python benchmark/cpu_smoke.py
--request-factory-engine /path/to/session_runner. It uses a strict local fake
completions server and a tiny local tokenizer. It verifies full prompt length
and distinct token IDs within each prompt. Fake-server throughput is not a
serving-performance result.
