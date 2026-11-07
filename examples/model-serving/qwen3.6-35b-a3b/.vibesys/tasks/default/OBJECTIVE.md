# Objective - Qwen3.6-35B-A3B 2xH100 Serving

Build an OpenAI-compatible serving system for `Qwen/Qwen3.6-35B-A3B` across
2 NVIDIA H100 80GB GPUs. There is no vLLM or SGLang checkout. The project root
holds a baseline engine, `engine/`, which `main.py` deploys on Modal as
`python -m engine.server`: Transformers runs the BF16 checkpoint sharded over
both GPUs, prefilling one request at a time and decoding running requests as
one batch, with a snapshot prefix cache. It runs and
meets the service contract, but it is unoptimized and is not a validated
performance baseline. Replace its pieces with your own: weight sharding, expert
routing and top-k dispatch, the fused grouped-GEMM MoE experts, the KV cache, a
request scheduler, and the HTTP server. `torch` and `transformers` are
available as utilities (weight loading, tokenizer, reference ops); the measured
serving path should become your own.

Qwen3.6-35B-A3B is a sparse Mixture-of-Experts model: 35B total parameters but
only ~3B activated per token (256 experts, 8 routed + 1 shared per token). At
bf16 the weights (~70 GB) fit a single H100, so serving across two GPUs is a
parallelism-strategy question rather than a capacity one. The core problems are
distributing the model across both GPUs (tensor parallelism, expert
parallelism, or a hybrid) and the cross-GPU expert-dispatch communication, the
MoE execution path (routing, grouped-GEMM kernels, memory-bandwidth-bound decode
with only ~3B of 35B active), request batching and scheduling, and KV-cache
management.

## Workload

Run the benchmark command configured by `vibesys.input.toml`. The evaluator's
versioned fixed-text entrypoint may pass a different serving URL:

```bash
python3 <EVALUATOR_PACKAGE>/fixed_text.py --request-factory-engine <RF_ENGINE> \
  --model Qwen/Qwen3.6-35B-A3B \
  --tokenizer Qwen/Qwen3.6-35B-A3B \
  --tokenizer-revision 995ad96eacd98c81ed38be0c5b274b04031597b0 \
  --request-count 64 \
  --input-tokens 256 --output-tokens 128 --concurrency 16 --url <SERVER_URL>
```

Default load:

- `/v1/completions`
- streaming responses
- 64 independent requests, saturated replay, maximum concurrency 16
- exact 256-token prompts and `max_tokens = 128`
- `temperature = 0`

This benchmark stresses the MoE dispatch path, cross-GPU parallelism, KV-cache
management, scheduler overhead, and decode throughput under concurrency.
Exact token lengths replace the approximate prompt word count, and the fixed
trace replaces the legacy 30-second closed-loop duration. Candidates must not
reduce request count, prompt/output lengths, or concurrency to improve the
score.

## Metrics

Pareto axes:

- `output_token_throughput_per_s`: RF-measured output tokens per second,
  maximize.
- `p90_latency_ms`: RF-measured end-to-end request latency in milliseconds,
  minimize.

These intentionally replace legacy `aggregate_throughput` (which counted
nonempty SSE chunks as tokens) and p99 latency. Old and new scores are not
directly comparable. The scalar headline metric is
`output_token_throughput_per_s`.

## Correctness

The accuracy checker drives the running server over HTTP with three
reference-free gates: sentinel echo, known-answer, and greedy determinism. It
requires a real prompt-conditioned Qwen3.6-35B-A3B forward pass. Canned
responses, prompt echoing, skipped model execution, or non-deterministic
temperature-0 decoding fail the task.
