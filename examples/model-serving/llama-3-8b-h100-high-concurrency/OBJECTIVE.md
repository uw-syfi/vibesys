# Objective - Llama 3.1 8B vLLM H100 High-Concurrency Serving

Optimize a vLLM-based OpenAI-compatible server for
`meta-llama/Llama-3.1-8B-Instruct` on a single NVIDIA H100.

The candidate workspace starts from a pinned vLLM source checkout declared in
`vibesys.input.toml`. Modify vLLM internals, server launch code, or runtime
configuration as needed, while preserving the public API and correctness gates.

## Workload

Run the benchmark exactly as written unless the evaluator passes a different
`--url` or `--output-json`:

```bash
uv run python benchmark/benchmark.py --url <SERVER_URL> --output-json <PATH>
```

Default load:

- `/v1/completions`
- streaming responses
- closed-loop concurrency 64
- 20 second duration
- short synthetic prompts with about 32 repeated words
- `max_tokens = 16`
- `temperature = 0`

This benchmark stresses request turnover, queueing, scheduling overhead, and
short decode steps. Candidates must not lower concurrency or cap admitted work
to manufacture lower latency at the expense of throughput.

## Metrics

Pareto axes:

- `aggregate_throughput`: output tokens per second, maximize.
- `p99_latency_ms`: end-to-end request latency in milliseconds, minimize.

The scalar fallback/headline metric is `aggregate_throughput`.

## Correctness

The accuracy checker drives the running server over HTTP. It requires a real
prompt-conditioned Llama forward pass through sentinel echo, known-answer, and
greedy-determinism gates. Canned responses, prompt echoing, skipped model
execution, or non-deterministic temperature-0 decoding fail the task.
