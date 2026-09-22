# Objective - Qwen3.5-397B-A17B Multi-Turn Serving From Scratch on 4x MI300A

Build a serving engine for `amd/Qwen3.5-397B-A17B-MXFP4` on one node with 4x
AMD MI300A (ROCm) that minimizes the p95 time-to-first-token of multi-turn chat
sessions. No serving engine is provided: you write the model execution, the
scheduler, the KV and recurrent-state management, and the HTTP server. Model
weights are on disk at `$MODEL_PATH`. How the model is sharded across the 4
GPUs (tensor, expert, pipeline, or a mix) is your choice.

Qwen3.5-397B-A17B is a sparse MoE (397B total, about 17B active per token)
that mixes Gated DeltaNet linear-attention layers, which carry recurrent
state, with full-attention layers, which carry a KV cache. Reusing prior-turn
work (prefix reuse) has to handle both kinds of state.

## Allowed and disallowed code

Allowed:

- `torch`, `triton`, and AITER used as a kernel library (import and call its
  kernels; do not use any serving engine built on it).
- `transformers`, `tokenizers`, `safetensors`, `numpy`, `aiohttp`, `fastapi`,
  `uvicorn`, and similar utilities for tokenization, chat templating, weight
  reading, and HTTP.
- The reference modeling code under `reference/`, as a readable specification.

### Disallowed engine code

Importing, copying, vendoring, translating file by file, or pip-installing
`sglang`, `vllm`, or the TensorRT-LLM engine (or any package that bundles
their scheduler, model runner, or model implementations) is forbidden. This is
a validity rule: the judge inspects the submission, and a round that uses
disallowed engine code is invalid regardless of its metric. Writing your own
kernels, or calling AITER kernels directly, is fine.

Judge checklist (any hit makes the candidate invalid, whatever its metric):

- An import of `sglang`, `sgl_kernel`, `vllm`, or `tensorrt_llm` in served code,
  including dynamic imports.
- Engine files copied, vendored, or translated file by file into the workspace,
  under any name (compare against your knowledge of those engines' schedulers,
  model runners, and model files, not just directory names).
- Engine installs in setup or requirements files, or in the run's logs.
- A server that only runs because an engine is present in the environment.

`accuracy_checker/engine_scan.py` runs first in the accuracy gate and rejects
the mechanical cases (imports, install lines, vendored directories). It cannot
see renamed copies, so the judge still reviews the source.

## Workload

The benchmark drives many concurrent multi-turn chat sessions against your
server. Each session is a short back-and-forth: a user message, your reply,
another user message appended after your own prior reply, and so on for 3 to 6
turns. Every request carries the session's full accumulated history (the real
text you returned earlier), so later turns send strictly more prompt tokens
than earlier ones. Between a session's turns there is a think-time pause of 1
to 8 seconds. Up to 48 sessions are in flight at once, started 0.4 s apart,
so the server sees sessions at different points of their conversation.

Decoding is greedy (`temperature` 0) with `ignore_eos`: each turn generates
exactly its `max_tokens` budget (80 to 300 tokens). The workload is fixed and
deterministic (`benchmark/run.py`); it is not yours to change.

## Metrics

Scored on a Pareto frontier over two axes (see `objectives.toml`), so a
candidate is retained when it is not dominated on both:

| Metric | Direction | Meaning |
| --- | --- | --- |
| `total_token_throughput` | maximize | Aggregate serving throughput in completion tokens per second: tokens generated across all concurrent sessions, over the whole run's wall clock. |
| `p95_ttft_turn2plus_ms` | minimize | 95th percentile time-to-first-token in milliseconds over every turn with index 2 or later in its session (each session's opening turn is excluded). |

Trading one against the other is the point: a decode speedup that raises
throughput while pushing the TTFT tail out does not dominate, and neither does
a TTFT win bought by serving fewer tokens per second.

Throughput saturates. The workload sends a fixed number of turns, each filling
a fixed output budget, so the numerator is a constant and the metric is
`constant / makespan`. Under the default scheduled pacing the sends are timed
off a fixed reference server speed, so once the server is fast enough for every
send to be schedule-bound, throughput converges to the rate the schedule offers
rather than to the server's own capacity. Past that point it says "the server
keeps up with the offered load" and stops separating candidates; the TTFT
percentile remains the fine-grained axis.

Also reported, not ranked: `mean_tpot_ms`, `turn1_ttft_ms` (mean TTFT of
opening turns), `schedule_bound_fraction`, `wall_duration_s`, and the echoed
workload settings. `benchmark/run.py` reports every metric twice: to the
framework as an evaluator result-protocol record stream (`--vs-output`), and to
a flat JSON file for humans (`--output-json`).

## Candidate contract

The seed `server.py` already follows this; keep it true.

- Entrypoint at the workspace root: `python3 server.py --model-path <dir>
  --host <h> --port <p>`. If `--model-path` is absent, read `MODEL_PATH`.
- Single node, 4 GPUs. Spawn whatever processes you need; the harness starts
  and stops the whole process group.
- `GET /health` returns 200 only when the model is loaded and requests can be
  served.
- `POST /v1/chat/completions`, OpenAI-compatible, with streaming SSE
  (`stream: true`, ending in `data: [DONE]`) and non-streaming. It must
  honor `max_tokens`, `temperature` 0 (greedy), `ignore_eos` (generate exactly
  `max_tokens` tokens, ignoring EOS), and `chat_template_kwargs`
  (`enable_thinking`, applied when rendering the model's chat template).
- Usage: the final streamed chunk (requested with
  `stream_options.include_usage`) and non-streaming responses report
  `usage.prompt_tokens` and `usage.completion_tokens`. Streamed content deltas
  carry text in `choices[0].delta.content`; TTFT is measured at the first
  non-empty delta.

The harness gives a generous startup timeout (5400 s). Weight loading speed
is out of scope for the metric.

## Correctness

Any failing check invalidates the round.

1. Accuracy gate (`accuracy_checker/checker.py`), thinking disabled: 13
   probes (6 held-out multi-turn sessions replayed greedily, 4
   history-recall probes, 3 arithmetic probes) plus a greedy-token pin check.
   The pins in `reference/pins.json` were produced by a reference transformers
   forward; at least 90 percent of the pins must match the first 32 generated
   tokens exactly, and the rest must not diverge before token 8. See
   `accuracy_checker/README.md`.
2. Benchmark integrity: a hard failure (nonzero exit, no metric) if any
   request is dropped, errored, returns an empty reply, or generates fewer or
   more completion tokens than its fixed budget.

## Scope

Edit anything in the workspace except `accuracy_checker/`, `benchmark/`,
`reference/`, this objective, and the model weights at `MODEL_PATH`. The
checker and benchmark are the only way the harness reaches your server, so the
OpenAI-compatible streaming API is the whole interface.
