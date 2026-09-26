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

Run the Request Factory benchmark through the bundle adapter. The evaluator may
pass a different server `--url`:

```bash
python3 benchmark/benchmark.py --request-factory-engine <RF_ENGINE> --url <SERVER_URL>
```

Default workload:

- 64 independent streamed `/v1/completions` requests
- saturated replay, concurrency 8
- 256 input tokens and 128 output tokens per request
- `temperature = 0`

The RF request driver owns request generation and measurement. Candidates must
not reduce request count, token lengths, concurrency, or output target to improve
the score. Prompt and output lengths are exact token counts, replacing the
legacy approximate word count and wall-clock duration window.

## Metrics

Pareto axes:

- `output_token_throughput_per_s`: RF-measured output tokens per second, maximize.
- `p90_latency_ms`: RF-measured end-to-end request latency in milliseconds, minimize.

These intentionally replace legacy `aggregate_throughput` (which counted
nonempty SSE chunks as tokens) and `p99_latency_ms`. Scores from the old and new
protocols are not directly comparable.

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
