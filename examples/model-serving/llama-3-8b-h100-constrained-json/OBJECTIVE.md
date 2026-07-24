# Objective - Llama 3.1 8B vLLM H100 Constrained JSON Serving

Optimize a vLLM-based OpenAI-compatible server for
`meta-llama/Llama-3.1-8B-Instruct` on a single NVIDIA H100 under JSON-schema
constrained decoding.

The candidate workspace starts from a pinned vLLM source checkout declared in
`vibesys.input.toml`. Modify vLLM internals, server launch code, guided decoding
code, or runtime configuration as needed, while preserving the public API and
schema correctness.

## Workload

Run the benchmark exactly as written unless the evaluator passes a different
`--url` or `--output-json`:

```bash
uv run python benchmark/benchmark.py --url <SERVER_URL> --output-json <PATH>
```

Default load:

- `/v1/completions`
- streaming responses
- closed-loop concurrency 16
- 20 second duration
- `guided_json` request body with a fixed profile schema
- short natural-language prompts
- `max_tokens = 96`
- `temperature = 0`

This benchmark stresses constrained decoding, grammar-state advancement,
allowed-token masking, scheduler interaction with grammar work, and JSON output
streaming. Candidates must not ignore the schema or replace generation with
hard-coded templates.

## Metrics

Pareto axes:

- `aggregate_throughput`: output tokens per second for schema-valid responses,
  maximize.
- `p99_latency_ms`: end-to-end request latency in milliseconds, minimize.

The scalar fallback/headline metric is `aggregate_throughput`.

## Correctness

The accuracy checker sends fresh sentinel-bearing JSON-schema requests and
validates every response with `json.loads` and `jsonschema`. A response must:

- parse as JSON,
- validate against the supplied schema,
- include the request-specific sentinel in a schema-valid string field, and
- arrive through the OpenAI-compatible streaming `/v1/completions` protocol.

Transport success alone is not enough. Any xgrammar/FSM issue that produces
invalid JSON or schema-invalid output fails correctness.
