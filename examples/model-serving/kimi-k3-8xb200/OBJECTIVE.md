# Objective - Kimi-K3 From-Scratch 8xB200 Serving

Build an OpenAI-compatible serving system for `moonshotai/Kimi-K3` from
scratch on 8x NVIDIA B200 192GB GPUs. No serving engine is provided: unlike
the `-sglang` variant, there is no vLLM or SGLang checkout. Implement the whole stack yourself: weight loading and sharding
across the 8 (or more) B200s, expert routing and top-k dispatch with the
all-to-all communication across 896 experts, the MXFP4-weight/MXFP8-activation
dequantization fused into the MoE grouped-GEMM path, Kimi Delta Attention
linear-attention state management, a request scheduler, and the
OpenAI-compatible HTTP server. `torch` and `transformers` are available as
utilities (weight loading, tokenizer, reference ops), not as a serving
engine.

Kimi-K3 is a frontier-scale sparse MoE model: 2.8T total parameters, ~104B
activated per token, across an extreme 896-expert pool (16 routed + 2 shared
per token). It replaces standard attention with Kimi Delta Attention plus
Attention Residuals, a linear-attention-family design with recurrent state
rather than a growing KV cache. Weights are natively MXFP4 with MXFP8
activations from quantization-aware training. The model is natively
multimodal and agentic with up to 1M tokens of context; this task serves the
text-only `/v1/completions` path.

At ~4 bits, the weights are ~1.4TB, which fits in 8xB200's 1536GB of HBM, but
headroom for KV/state cache and activations is tight. Single-node 8xB200 is
at the edge of this model's footprint; a multi-node deployment (e.g. 16xB200)
may be required to serve long-context requests with adequate cache headroom.
The candidate must serve the model across all 8 GPUs (tensor parallelism,
expert parallelism, pipeline parallelism, or a hybrid) and may extend to
multiple nodes if needed.

This is a decode-heavy, memory-bandwidth-bound workload. The core problems
are: sharding 2.8T parameters across 8 (or more) B200s, fusing
MXFP4/MXFP8 dequantization into the MoE grouped-GEMM path on Blackwell,
managing Kimi Delta Attention's recurrent state, routing and load-balancing
across the 896-expert pool, and all-to-all dispatch efficiency at this scale
of expert parallelism.

## Workload

Run the Request Factory benchmark with its configured workload unless the
evaluator passes a different `--url`:

```bash
uv run python benchmark/benchmark.py --request-factory-engine <RF_ENGINE> --url <SERVER_URL>
```

Default load:

- `/v1/completions`
- streaming responses
- 256 independent requests at saturated concurrency 32
- long synthetic prompts (~4096 tokens)
- `max_tokens = 2048`
- `temperature = 0`

This benchmark stresses the MoE dispatch path across 896 experts, Kimi Delta
Attention state management, KV/state cache capacity, scheduler overhead, and
sustained long-decode throughput under concurrency. Candidates must not
reduce request count, prompt length, concurrency, or max output tokens to
improve the score. The prior 120-second rolling window is replaced with a fixed
RF trace, so scores are not directly comparable.

## Metrics

Pareto axes:

- `output_token_throughput_per_s`: RF-measured output token IDs per second,
  maximize.
- `p90_latency_ms`: RF end-to-end request latency in milliseconds, minimize.

The scalar fallback/headline metric is `output_token_throughput_per_s`.

## Correctness

The accuracy checker drives the running server over HTTP with three
reference-free gates: sentinel echo, known-answer, and greedy determinism. It
requires a real prompt-conditioned Kimi-K3 forward pass. Canned responses,
prompt echoing, skipped model execution, or non-deterministic temperature-0
decoding fail the task. The greedy-determinism gate uses a short `max_tokens`
to keep the check fast.
