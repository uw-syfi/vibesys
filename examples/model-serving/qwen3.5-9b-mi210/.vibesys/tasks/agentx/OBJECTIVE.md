# Qwen3.5-9B on MI210: AgentX serving

Optimize a real bf16 `Qwen/Qwen3.5-9B` serving engine on one AMD MI210 for
AgentX coding-session replay. Improve the Pareto frontier of **output tokens/s
(maximize)** and **TTFT p50/p90 and TPOT p50/p90 in ms (minimize)**, with a
3% relative noise tolerance, matching the Request Factory task
([PR #3674](https://github.com/uw-syfi/vibesys/pull/3674)). Report p90 request
latency as an additional diagnostic. TPOT maps to AgentX `inter_token_latency`,
computed per request as `(request_latency - TTFT) / (output_tokens - 1)`.
Neither shorter responses nor failed requests count as a performance improvement.

This task shares `engine/` and the trusted HF-golden accuracy checker with the
`default` Request Factory task. The seed engine currently exposes only
`/v1/completions`, has no prefix cache, and defaults to 16K context: it does
**not** yet pass AgentX. Implement streaming `/v1/chat/completions`, the Qwen
chat template, correct usage accounting, and long-context/state management as
part of the candidate. Preserve `/v1/completions`, including the token IDs and
logprobs used by the existing accuracy checker. Do not substitute vLLM or
SGLang for the candidate engine; the vLLM comparison is external.

## Fixed workload

The `vibesys-evaluator-agentx` package runs `agentx-harness==1.0.7`, scenario
`agentx`, and the date-pinned public corpus
`semianalysis_cc_traces_weka_062126` (Weka coding traces with subagents).
Tokenizer revision: `c202236235762e1c871ad0ccb60c8ee5ba337b9a`.

- Four concurrent session trees; children are scheduled by the upstream
  harness and can create more than four in-flight requests.
- Qwen's native 262,144-token context; upstream filtering excludes trajectories
  exceeding that capacity. Do not lower the capacity to discard harder traces.
- 900 seconds of measurement, seed 42, trajectory start ratios 0.25–0.75,
  ten warmup requests per lane, and a 300-second per-trace idle cap (the
  `agentx` preset). Preparation, warmup and drain are outside measurement.
- Streaming chat, full requested output lengths (`ignore_eos`), upstream
  parent/child dependencies and recorded pacing, and first-turn prefix cache
  busting. The scenario owns these invariants; no arbitrary extra CLI flags
  are forwarded by the evaluator.

The 900-second measurement is a task-specific AgentX recipe, shorter than the
`agentx` preset's one-hour default. Compare only identical recipes, model,
precision, hardware, context cap and concurrency. These scores are not directly
comparable with Request Factory or published AgentX results using other recipes.

## Acceptance

The existing accuracy gate must pass. The evaluator additionally requires
`metadata.submission_valid == true`, at least one successful measured request,
zero measured errors, finite nonnegative latency values, and positive output
throughput. Missing exports, incorrect units, cancellation, nonzero harness
exit, and deadline expiry produce errors without scored results. A valid
AgentX replay is a serving-performance check, not a coding-quality evaluation.
Do not change weights, precision, generation lengths, dataset, replay rules,
or accounting to improve the score.

Use the manifest's full mode for optimization and acceptance. A direct
`--mode smoke` run uses two eligible corpus entries, one warmup request per
lane and 60 seconds of measurement with upstream `--unsafe-override`.
It is diagnostic only: it never emits a VibeSys result, including when given
`--vs-output`. Never use smoke numbers to select a winner.

See the [upstream recipe](https://github.com/SemiAnalysisAI/agentx-harness/blob/master/docs/tutorials/agentx-mvp.md)
for replay semantics. The package version, rather than upstream `master`,
defines this task's executable behavior.
