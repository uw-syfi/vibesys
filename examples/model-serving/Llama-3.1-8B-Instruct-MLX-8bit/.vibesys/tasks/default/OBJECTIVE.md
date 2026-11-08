# Objective - Llama-3.1-8B-Instruct MLX 8-bit JSON-schema server

Minimize **end-to-end JSON generation latency** on local Apple Silicon while
keeping the JSONSchemaBench accuracy checker within tolerance. The candidate is
an OpenAI-compatible `/v1/completions` server for
`mlx-community/Meta-Llama-3.1-8B-Instruct-8bit` (revision
`142d428004044c37c441272c91316251d9aecc58`), the `engine/` package at the
project root. It starts as a working baseline: mlx-lm model execution with
llguidance token masks, one request at a time.

## Launch contract

The check, the benchmark and the profile load each start the server from the
checkout's root, wait for it, ask it, and stop it. The command is fixed:

```
uv run --project <root> python -m engine.server \
    --model-dir <snapshot> --host 127.0.0.1 --port <free port>
```

- The package is `engine`, and its `server` module is the entry point. Keep
  those names; everything else under `engine/` is yours.
- The server runs in the environment of the project root's `pyproject.toml`
  (and `uv.lock`). Add dependencies there. The judge has an environment of its
  own, in `.vibesys/tasks/default/`, which you cannot change.
- `--model-dir` is the pinned model's snapshot in the Hugging Face cache. Load
  weights from it; do not download.
- `GET /health` must answer 2xx once the server can serve a completion. The
  server has 300 s from its start to do so. Load time counts toward the job's
  time limit, not toward the latency.
- Every job's server is a new process. A job that is stopped ends the server's
  process group, so do not detach children from it.

## Workload

The benchmark sends streaming completion requests whose prompts contain a JSON
Schema from `epfl-dlab/JSONSchemaBench` and whose request body includes:

- `prompt`: plain text instructions plus the schema (its properties in the
  order the schema lists them, which the grammar of a constrained decoder
  usually requires too), and for a schema that can hold a string, a sentinel
  token the answer must include.
- `max_tokens`: 512.
- `temperature`: 0.
- `stream`: true.
- `response_format`: `{"type": "json_schema", "json_schema": {"schema": ...}}`.

The schemas are the dataset's own splits, pinned to revision
`5bd0f4640badc6f3f02df796421d21cb0ca0b141`, without the schemas of more than
1500 characters (444 of the 937 in `val` remain). **The loop is scored on
`val`. `test` is held out for final reporting (`bench.py holdout`, run on a
final or promoted commit), and you may use `train` freely for your own
testing.** The cap is about time, not about the decoder: at 6000 characters
(708 schemas) the baseline's check took 267 s and one benchmark sample 211 s,
and a sample may take about 3 minutes; at 1500 characters they took 177 s and
90 s. A schema that a decoder cannot compile stays in the pool and counts as a
failed request.

A fixed pseudo-random order, seeded by `42` and each schema's id, picks which
schemas: the benchmark asks ranks 0 to 23 of `val` and the check ranks 24 to 63,
so they never share a schema. The schemas are the same on every run; the
sentinel in each prompt is not (it is drawn from the operating system's
randomness at run time), so no answer can be memorized. Optimize for this
schema-constrained generation workload, not for general chatbot serving.

## Headline metric

`latency_ms.p50` of `bench.py benchmark`, in milliseconds. Lower is better. A
sample asks its 24 requests one after another (a closed loop), after one
request that is not counted and compiles the Metal kernels. A sample is a
measurement only if every request completes and the answers pass the same gate
as the check. `ttft_ms.p50`, `tpot_ms.p50` (a chunk is about one token),
`token_throughput` and `schema_ok_frac` are diagnostics.

## Server contract

- `/v1/completions` accepts the fields above and streams Server-Sent Events with
  OpenAI-style completion chunks whose `choices[0].text` field contains
  non-empty deltas, ending with `data: [DONE]`.
- The check passes when no request fails and:
  - every answer that ended is JSON that validates against the supplied schema;
  - at most 10% of the answers are cut off at `max_tokens`. The judge decides
    that itself: an invalid answer of at least 320 tokens, counted with the
    pinned model's tokenizer, was cut off, and a shorter one ended. The
    server's `finish_reason` is not read, as the candidate is not trusted;
  - at least 80% of the answers that ended, to schemas that can hold a string,
    include the sentinel.

  The benchmark holds its answers to the same gate before it reports a number.
  The baseline's answers: 40 answers, 39 valid, 1 cut off, none that ended invalid, and the sentinel in 30 of 34.
- The sentinel is a fresh token in the prompt. Do not hard-code schema-only
  templates or ignore prompt content.
- `/v1/chat/completions` may be implemented for compatibility, but the checker
  and benchmark target `/v1/completions`.

## Speed of light

Decode is bound by memory bandwidth: every token reads all the weights once.
On an M3 Pro (about 150 GB/s) the 8.5 GB of 8-bit weights give a floor of about
57 ms per token. The baseline measured 66.5 ms per token (the median of one sample, with the
GPU to itself), 17% above the floor, so a faster kernel has little left to win. The gains are in fewer model calls:
tokens the schema forces (punctuation, keys, jump-forward), speculative
decoding with the 1B draft model, and prefill, which is the baseline's
1043 ms to the first token (median).

## Optimization guidance

- Use MLX-native model execution for the 8-bit Llama 3.1 target; CUDA-specific
  optimizations do not apply on this target.
- JSON-schema constraints are the main algorithmic opportunity. The baseline
  applies an llguidance token mask to every step. Build or reuse a grammar /
  automaton that can force deterministic punctuation, object keys, and other
  schema-implied runs without calling the model for every output token.
- Speculative decoding is optional but relevant. The paper setup used
  `mlx-community/Llama-3.2-1B-Instruct-4bit` as a separate draft model against
  this 8-bit target; keep draft-model setup configurable rather than baked into
  the request contract.
- Tune MLX prefill and decode settings for the prompt lengths in
  JSONSchemaBench. Overly small prefill chunks can dominate latency on the long
  prompts.
- Keep output validation honest: optimizations must preserve schema validity,
  sentinel inclusion, and streaming semantics.
