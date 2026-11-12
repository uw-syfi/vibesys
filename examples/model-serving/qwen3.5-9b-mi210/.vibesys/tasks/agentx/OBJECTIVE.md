# Qwen3.5-9B on MI210: AgentX serving (vibesys-cli)

Optimize a real bf16 `Qwen/Qwen3.5-9B` serving engine on one AMD MI210 for
AgentX coding-session replay. Improve the Pareto frontier of **output tokens/s
(maximize)** and **TTFT p50/p90 and TPOT p50/p90 in ms (minimize)**, with a
3% relative noise tolerance. p90 request latency is reported as a diagnostic.
TPOT is AgentX's `inter_token_latency`. Neither shorter responses nor failed
requests count as an improvement.

This is a two-image task that vibesys-cli runs (`version = "0.2"`):

- **The candidate** is `engine/` and the image `engine/Dockerfile` builds from
  your commit. You own both: any base, ROCm userspace, kernel library or
  profiler you need. The server starts with
  `python3 -m engine.server --model Qwen/Qwen3.5-9B --port $PORT` from the
  checkout root and must answer `GET /v1/models` within 30 minutes. Model
  weights come from the operator's Hugging Face cache mount; never bake them
  into the image.
- **The task image** (this directory) runs the judge and the load over HTTP,
  with no GPU. Nothing you install changes it.

The seed engine exposes only `/v1/completions`, has no prefix cache and does
**not** yet pass AgentX: implement streaming `/v1/chat/completions`, the Qwen
chat template, correct usage accounting and long-context state management as
part of the candidate. Do not substitute vLLM or SGLang for the engine.

## Accuracy (`check`)

`golden.json` holds HF transformers bf16 reference outputs for 12 prompts
(token ids, greedy continuations, and per-position teacher-forced logprobs,
top-1 tokens and margins; source in `meta`). The gate never requires
bit-exact tokens, only the same distribution: free-run divergences only at
near-ties (reference margin < 0.5 nats), no decisive teacher-forced argmax
flips, mean |Δlogprob| ≤ 0.05 and p99 ≤ 0.25, mean matched-prefix fraction
≥ 0.5, the same rules over chained session rounds (the prefix-cache resume
path), and a streamed response equal to the non-streamed one with a correct
usage chunk. The server needs `/v1/completions` with `ignore_eos`,
`return_token_ids`, `echo` + `logprobs` + `return_tokens_as_token_ids`, and
`usage.prompt_tokens_details.cached_tokens`. The gate is
`vibesys_evaluator.llm_serving.golden` in the task image (its bounds are
`golden.Thresholds`; the requests it sends are in that module), and
`golden.json`'s `meta` says how the reference was made. This task does not
depend on any other task directory, so it still reads true when copied alone.

## Benchmark (`benchmark`)

`agentx-harness==1.0.7` (pinned by the vibesys-evaluator llm-serving image),
scenario `agentx`, the date-pinned corpus `semianalysis_cc_traces_weka_062126`,
tokenizer revision `c202236235762e1c871ad0ccb60c8ee5ba337b9a`, four
concurrent session trees, the native 262,144-token context, 900 seconds of
measurement after ten warmup requests per lane, seed 42 and trajectory start
ratios 0.25–0.75. A result requires a valid submission, at least one measured
request, zero errors (an explicit empty error summary), the expected units
and finite non-negative values; anything else is an error, never a score.

## Profile (`profile`)

A 60-second smoke replay of two corpus entries, a load to record under a
profiler. It is never scored; never use its numbers to select a candidate.
