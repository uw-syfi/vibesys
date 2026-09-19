# Llama-3.3 70B Dense Multi-GPU Serving

Build an OpenAI-compatible chat/completion server from scratch for
`meta-llama/Llama-3.3-70B-Instruct` on 2x NVIDIA H100 80 GB GPUs. The model
is ~140 GB in bf16, so it does not fit in one GPU; the implementation must
serve it across both devices. All model parameters must be served (no layers,
heads, or precision silently dropped).

The parallelization strategy (tensor parallelism degree, pipeline parallelism,
etc.) is left to the implementation. The contract constrains observable output
and the model served, not how work is split.

## Workload

Run the benchmark exactly as written unless the evaluator passes a different
`--url` or `--output-json`:

```bash
uv run python .vibesys/tasks/default/benchmark/benchmark.py --url <SERVER_URL> --output-json <PATH>
```

Default load:

- `/v1/completions` and `/v1/chat/completions`
- streaming responses
- closed-loop concurrency 8
- 30 second duration
- synthetic prompts ~256 words
- `max_tokens = 128`
- `temperature = 0`

Standard chat/completion workload. Candidates must not reduce prompt length,
duration, concurrency, or max output tokens to improve the score.

## Metrics

Pareto axes:

- `aggregate_throughput`: output tokens per second, maximize.
- `p99_latency_ms`: end-to-end request latency in milliseconds, minimize.

The scalar fallback/headline metric is `aggregate_throughput`.

## Correctness

The accuracy checker drives the running server over HTTP with three
reference-free gates that a real Llama forward pass passes and reward-hacking
shortcuts (canned text, prompt echoers, schema synthesizers) fail:

1. Sentinel-echo rate: each request embeds a random sentinel token the prompt
   instructs the model to reproduce. A server that ignores the prompt and
   returns canned/templated text cannot reproduce a fresh random token.

2. Known-answer rate: near-deterministic factual prompts at temperature 0
   whose answer is fixed (capital of France -> Paris, 1+1 -> 2, ...). A prompt
   echoer passes the sentinel gate but fails this one; a canned "Paris" server
   fails the sentinel gate. Only a model that actually runs inference passes
   both.

3. Greedy determinism: the same prompt sent twice at temperature 0 must yield
   identical output. Catches nondeterministic / sampling-when-it-should-not
   decoders.

The checker requires a real Llama forward pass. Canned responses, prompt
echoing, skipped model execution, or non-deterministic temperature-0 decoding
fail the task.
