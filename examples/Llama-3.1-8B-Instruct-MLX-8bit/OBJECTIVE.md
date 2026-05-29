# Objective - Llama-3.1-8B-Instruct MLX 8-bit JSON-schema server

Minimize **end-to-end JSON generation latency** on local Apple Silicon while
keeping the JSONSchemaBench accuracy checker within tolerance. Build an
OpenAI-compatible `/v1/completions` server for
`mlx-community/Meta-Llama-3.1-8B-Instruct-8bit`.

## Workload

The benchmark sends streaming completion requests whose prompts contain a JSON
Schema from `epfl-dlab/JSONSchemaBench` and whose request body includes:

- `prompt`: plain text instructions plus the schema.
- `max_tokens`: normally 256.
- `temperature`: 0.
- `stream`: true.
- `response_format`: `{"type": "json_schema", "json_schema": {"schema": ...}}`.

The default benchmark dataset is the `full` subset, `val` split, pinned to
revision `5bd0f4640badc6f3f02df796421d21cb0ca0b141`. Optimize for this
schema-constrained generation workload, not for general chatbot serving.

## Headline metric

Use `latency_ms.p50` from `benchmark/benchmark.py`'s JSON output as the primary
performance metric. Lower is better. `token_throughput`, `ttft_ms`, and
`tpot_ms` are useful diagnostics, but the objective is closed-loop
end-to-end latency for valid JSON responses.

## Server contract

- `/health` returns 200 when the server is ready.
- `/v1/completions` accepts the fields above and streams Server-Sent Events with
  OpenAI-style completion chunks whose `choices[0].text` field contains
  non-empty deltas.
- The response text must parse as JSON and validate against the supplied schema.
- For schemas that can hold strings, the checker injects a sentinel token into
  the prompt and requires the generated JSON to include it. Do not hard-code
  schema-only templates or ignore prompt content.
- `/v1/chat/completions` may be implemented for compatibility, but the checker
  and benchmark target `/v1/completions`.

## Optimization guidance

- Use MLX-native model execution for the 8-bit Llama 3.1 target; CUDA-specific
  optimizations do not apply on this target.
- JSON-schema constraints are the main algorithmic opportunity. Build or reuse a
  grammar / automaton that can force deterministic punctuation, object keys, and
  other schema-implied runs without calling the model for every output token.
- Speculative decoding is optional but relevant. The paper setup used
  `mlx-community/Llama-3.2-1B-Instruct-4bit` as a separate draft model against
  this 8-bit target; keep draft-model setup configurable rather than baked into
  the request contract.
- Tune MLX prefill and decode settings for the prompt lengths in
  JSONSchemaBench. The prompts are often around the low-thousands of tokens, so
  overly small prefill chunks can dominate latency.
- Keep output validation honest: optimizations must preserve schema validity,
  sentinel inclusion, and streaming semantics.
