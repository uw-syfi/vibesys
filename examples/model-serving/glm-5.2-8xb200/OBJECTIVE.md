# Objective - GLM-5.2 From-Scratch 8xB200 Serving

Build an OpenAI-compatible serving system for `zai-org/GLM-5.2` from scratch
across 8x NVIDIA B200 192GB GPUs. No serving engine is provided: no vLLM or
SGLang checkout. Implement the whole stack yourself:
weight loading and sharding across all 8 GPUs (tensor/expert/pipeline
parallelism or a hybrid), FP8 grouped-GEMM MoE experts and dequant, expert
routing/top-k dispatch and the all-to-all communication it requires, the
sparse-attention mechanism for long context, a KV cache sized for long
contexts, a request scheduler, and the HTTP server. `torch` and `transformers`
are available as utilities (weight loading, tokenizer, reference ops), not as
a serving engine.

GLM-5.2 is a large sparse Mixture-of-Experts model: 753B total parameters,
~40B active per token, with a sparse-attention mechanism (`glm_moe_dsa`) for
its 1M-token context window. At bf16 the weights (~1.5 TB) do not fit 8xB200
(1536 GB total) with room for KV cache, so the candidate must serve in FP8
(~753 GB), building its own FP8 grouped-GEMM MoE kernels. The core problems
are distributing the model across 8 GPUs, the cross-GPU expert-dispatch
communication (all-to-all, expert load balancing), the sparse long-context
attention path, request batching and scheduling, and KV-cache management
under concurrency, not just raw weight sharding.

## Workload

Run the Request Factory benchmark with its configured workload unless the
evaluator passes a different `--url`:

```bash
uv run python benchmark/benchmark.py --request-factory-engine <RF_ENGINE> --url <SERVER_URL>
```

Default load:

- `/v1/completions`
- streaming responses
- 256 independent requests at saturated concurrency 64
- long synthetic prompts (8192 token IDs), with no shared prefix
- `max_tokens = 1024`
- `temperature = 0`

This benchmark stresses the sparse-attention long-context path, FP8 MoE
dispatch across 8 GPUs, KV-cache management under concurrency, and decode
throughput. Candidates must not reduce request count, prompt length,
concurrency, or max output tokens, and must not collapse requests into a shared
cached prefix, to improve the score. The former 120-second rolling window is
replaced by a fixed RF trace, so scores are not directly comparable.

## Metrics

Pareto axes:

- `output_token_throughput_per_s`: RF-measured output token IDs per second,
  maximize.
- `p90_latency_ms`: RF end-to-end request latency in milliseconds, minimize.

The scalar fallback/headline metric is `output_token_throughput_per_s`.

## Correctness

The accuracy checker drives the running server over HTTP with three
reference-free gates: sentinel echo, known-answer, and greedy determinism. It
requires a real prompt-conditioned GLM-5.2 forward pass. Canned responses,
prompt echoing, skipped model execution, or non-deterministic temperature-0
decoding fail the task.
