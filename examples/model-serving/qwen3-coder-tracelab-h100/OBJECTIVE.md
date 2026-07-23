# Objective - Qwen3-Coder TraceLab H100 serving

Maximize **TraceLab replay output throughput** for `Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8` served by a vLLM-compatible OpenAI `/v1/completions` endpoint on a single Modal **H100**. The workload is coding-agent serving, not generic chat: multi-round sessions, growing prompts, short-ish model outputs, repeated prefixes, tool waits, and concurrent active sessions.

**Headline metric**: `aggregate_output_tokens_per_second` from `benchmark/benchmark.py` JSON output. Higher is better.

## Workload Contract

- The framework-owned benchmark uses TraceLab's real public collected trace data and TraceLab's own `session_runner`. The runner and trace release are hidden from the candidate workspace.
- Requests are session-ordered and closed-loop: the next round in a session starts only after the previous model response completes and the trace-derived tool wait elapses.
- Each prompt is sent as integer token IDs so prefix-cache keys are stable and server-side chat templating cannot perturb the benchmark.
- The benchmark asks for streaming completions, `temperature=0`, `ignore_eos=true`, `return_token_ids=true`, and `stream_options.include_usage=true`.
- The server must report cached prompt tokens via vLLM-compatible usage details; TraceLab's runner performs a prefix-cache preflight and fails fast when prompt-cache accounting is unavailable.
- Accuracy must pass `accuracy_checker/checker.py`; benchmark optimizations must still run the target model rather than returning canned completions.

## Optimization Direction

Specialize for TraceLab-like traffic even if out-of-distribution traffic suffers. Good implementation directions include:

- Start from vLLM's OpenAI server or a thin vLLM embedding, with `--enable-prefix-caching`, `--enable-prompt-tokens-details`, and H100-friendly dtype/quantization settings for Qwen3-Coder FP8.
- Keep session KV blocks alive across short tool waits and human-paced gaps; a 5-10 minute retention target matches TraceLab's observed prefix-cache knee.
- Make prefill append-length aware: when a session continues, avoid redoing long prefixes and pay mainly for the new append.
- Prioritize TTFT and throughput for long-prefix, short-output requests over generic batching fairness.
- If the implementation wraps vLLM internals, optimize scheduler admission around active session continuations and prefix-cache hit probability.

Do not weaken the benchmark, ignore the prompt, bypass model execution, or hard-code benchmark outputs.
