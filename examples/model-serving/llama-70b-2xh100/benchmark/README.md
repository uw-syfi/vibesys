# Multi-GPU Chat/Completion Benchmark

Request Factory drives streamed `/v1/completions` with eight concurrent
requests, 256-token synthetic prompts, and 128-token output targets. The trace
contains 64 independent requests and RF runs it at saturation. The exact token
lengths replace the old approximate 256-word prompt, and the fixed trace replaces
the old 30-second duration window.

The VibeSys protocol-v2 metrics are:

- `output_token_throughput_per_s`: RF's measured output-token throughput,
  replacing the old count of nonempty SSE chunks per second.
- `p90_latency_ms`: RF's p90 end-to-end request latency, replacing the old p99
  computed by the bundle driver.

RF sends token-ID prompts using the served model tokenizer, sets
`ignore_eos=true`, and measures completion token IDs rather than inferring token
count from SSE chunk count. The adapter generates a deterministic temporary text
corpus and bounds RF's token pool to at least twice the longest prompt (and the
request count), so the prompt data is sufficient for the configured workload
without a committed corpus fixture. There is no warmup phase in this workload. These
methodology changes are intentional; do not compare the new score numerically
with legacy benchmark results as if they were the same metric.

For CPU-only request-path validation, point the benchmark at a strict fake
OpenAI completions server and pass a local tokenizer fixture plus short token
lengths. Fake-server throughput is not a serving-performance result.
