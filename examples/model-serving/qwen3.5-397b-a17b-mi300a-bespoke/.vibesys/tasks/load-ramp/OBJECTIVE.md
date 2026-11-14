# Objective - Qwen3.5-397B-A17B load ramp, from scratch on 4x MI300A

Build a serving engine for `amd/Qwen3.5-397B-A17B-MXFP4` on one node of four AMD
MI300A APUs (ROCm) that serves the most multi-turn chat tokens per second at high
concurrency while keeping the time to first token of follow-up turns low. No serving
engine is provided: you write the model execution, the scheduler, the KV and recurrent
state management, and the HTTP server. How the model is split across the four APUs
(tensor, expert, pipeline, or a mix) is your choice.

Qwen3.5-397B-A17B is a sparse MoE (397B total, about 17B active per token) that mixes
Gated DeltaNet linear-attention layers, which carry recurrent state, with full-attention
layers, which carry a KV cache. Reusing prior-turn work (prefix reuse) has to handle both
kinds of state. The reference modeling code, the checkpoint's tensor layout and the
architecture summary are in `.vibesys/tasks/default/reference/` (read it, do not import it
at runtime).

## Allowed and disallowed code

Allowed:

- `torch`, `triton`, and AITER used as a kernel library (import and call its kernels; do
  not use any serving engine built on it).
- `transformers`, `tokenizers`, `safetensors`, `numpy`, `aiohttp`, `fastapi`, `uvicorn`,
  and similar utilities for tokenization, chat templating, weight reading, and HTTP.
- The reference modeling code, as a readable specification.

Disallowed: importing, copying, vendoring, translating file by file, or installing
`sglang`, `vllm`, or the TensorRT-LLM engine (or any package that bundles their scheduler,
model runner, or model implementations). The image `Dockerfile.rocm` builds starts from a
ROCm image that ships SGLang and removes it; keep it removed. Writing your own kernels, or
calling AITER kernels directly, is fine.

The checker's engine scan (`accuracy_checker/engine_scan.py`) runs first and fails the
check on an engine import, an install line in a requirements file or Dockerfile, or a
vendored engine directory. It cannot see a renamed copy, so a reviewer still reads the
source: any of these makes a candidate invalid whatever its numbers.

- An import of `sglang`, `sgl_kernel`, `vllm`, or `tensorrt_llm` in served code,
  including dynamic imports.
- Engine files copied, vendored, or translated file by file into the checkout, under any
  name.
- An engine installed by a setup file, a requirements file or a Dockerfile.
- A server that only runs because an engine is present in the image.

## Workload

`benchmark/run.py` (benchmark version 6) drives an unbounded, deterministic stream of
multi-turn chat sessions (seed 20260225) at a fixed number of requests in flight: C16,
then C96. Each session has 3 to 6 turns. Every request carries the session's whole
history (the text your server returned earlier), so later turns send strictly more
prompt tokens. User messages are 30 to 300 words; each turn generates exactly its budget
of 80 to 300 tokens (greedy, `ignore_eos`); a session pauses 1 to 8 seconds between turns
while another session keeps the concurrency full.

Each level runs a 15 s warmup, then a 60 s measurement window, extended in 60 s steps (to
at most 240 s) until at least `max(20, 2 * C)` turns were sent in it. After the last level
the workers drain, and every level is recomputed with the turns that were in flight at
its window's end.

The ramp stops early when a level breaches a latency guardrail (turn-2+ TTFT p95 over
10 s, or time per output token p95 over 250 ms). An invalid level (any failed, dropped,
empty, or short turn) stops the ramp too, and fails the whole run when it is C16.

## Metrics

Scored together on a Pareto frontier (`objectives.toml`); a candidate is kept when no
other one is better on both:

| Metric | Direction | Meaning |
| --- | --- | --- |
| `peak_goodput_tok_s` | maximize | Completion tokens per second in the measurement window, best over the levels that are valid and within the guardrails (or over the valid levels, when none is within them). An engine that serves C96 within the guardrails scores its C96 throughput. |
| `p95_ttft_turn2plus_ms_at_ref` | minimize | 95th percentile time to first token, in ms, over turn 2 and later of each session, at C16. |

Trading one against the other is the point: batching that raises C96 throughput while
pushing the C16 follow-up TTFT out does not dominate, and neither does a TTFT win bought
by serving fewer tokens per second. Repeated runs of one build on one node differed by
about 6 percent, which is the frontier's noise floor.

Also reported, not ranked: each level's throughput, turn-2+ TTFT p95 and TPOT p95
(`throughput_tok_s_c16`, `p95_ttft_turn2plus_ms_c96`, ...) for each level that ran, and
the per-level table and per-turn records in the job's artifacts.

## Candidate contract

The seed `server.py` follows it; keep it true.

- Built by `Dockerfile.rocm` at the checkout's root, run from the checkout's root as
  `python3 server.py --model-path /vibesys/mounts/qwen3-5-397b-a17b-mxfp4 --host 127.0.0.1
  --port <port>`, with the node's four APUs. Spawn whatever processes you need.
- `GET /health` returns 200 only when the model is loaded and requests can be served. The
  server has 7,200 s to get there; weight loading is out of the metric.
- `POST /v1/chat/completions`, OpenAI compatible, streaming (`stream: true`, SSE ending
  in `data: [DONE]`) and non-streaming. It honors `max_tokens`, `temperature` 0 (greedy),
  `ignore_eos` (exactly `max_tokens` tokens), and `chat_template_kwargs`
  (`enable_thinking`, applied when rendering the model's chat template). Streamed text is
  in `choices[0].delta.content`; TTFT is the first non-empty delta. The final streamed
  chunk (with `stream_options.include_usage`) and non-streaming replies report
  `usage.prompt_tokens` and `usage.completion_tokens`.
- `POST /v1/score`, always served: body `{"prompt": [token ids], "continuation_start": n,
  "top_k": k}` with `0 < n < len(prompt)`. Reply `{"scores": [...]}` with one row per
  position `n .. len(prompt) - 1`, in order; row `j` is the distribution the model predicts
  for `prompt[n + j]` given `prompt[: n + j]`, as `{"token_id": prompt[n + j], "logprob":
  ..., "top_logprobs": [{"token_id": ..., "logprob": ...}, ...]}`, the `k` most likely
  tokens first. It must run the same numerics as the serving path.

## Correctness

The check (`accuracy_checker/checker.py`, thinking disabled) fails the candidate on any of:

1. The engine scan above.
2. 13 behavioral probes: 6 held-out multi-turn sessions replayed greedily (non-empty
   replies, sane finish reasons, `usage.prompt_tokens` growing with the history), 4
   recalls of a fact planted in turn 1, and 3 arithmetic prompts.
3. The numerics gate the optimization campaign accepted every change by, with its
   thresholds, against a transformers reference (`reference/`, see its README):
   - teacher-forced top-1: each of 34 reference prompts with its 32-token reference
     continuation goes through `/v1/score`; at least 1,074 of the 1,088 positions must
     have the reference's top-1 token as your top-1 token, and every position must be
     scored;
   - greedy: each of 5 reference prompts, greedy with natural EOS and at most 64 tokens,
     must return exactly the reference's text.

The benchmark fails the run when any C16 turn is dropped, errors, is empty, or generates
a different number of tokens than its budget.

## Rules

- No state carried across requests other than what a real server keeps for its sessions
  (prefix and recurrent-state reuse of a session's own history is fine); nothing that
  recognizes the benchmark's or the checker's prompts, seeds, or session shapes.
- The scored path and `/v1/score` run the same model: do not serve the checker from a
  different precision, kernel set, or code path than the benchmark sees.
- Do not lower the output: every turn generates its whole budget of real tokens.

## Scope

Change anything in the checkout except `.vibesys/` (the checker, the benchmark, the
reference, and this objective) and the model weights. The checker and the benchmark reach
your server only through the HTTP interface above.
