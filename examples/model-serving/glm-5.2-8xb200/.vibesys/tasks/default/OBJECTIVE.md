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

Run the benchmark exactly as written unless the evaluator passes a different
`--url` or `--output-json`:

```bash
uv run python .vibesys/tasks/default/benchmark/benchmark.py --url <SERVER_URL> --output-json <PATH>
```

Default load:

- `/v1/completions`
- streaming responses
- closed-loop concurrency 64
- 120 second duration
- long synthetic prompts (~8192 tokens), distinct per request rather than a
  shared prefix
- `max_tokens = 1024`
- `temperature = 0`

This benchmark stresses the sparse-attention long-context path, FP8 MoE
dispatch across 8 GPUs, KV-cache management under concurrency, and decode
throughput. Candidates must not reduce prompt length, duration, concurrency,
or max output tokens, and must not collapse the distinct per-request prompts
into a single cached prefix, to improve the score.

## Metrics

Pareto axes:

- `aggregate_throughput`: output tokens per second, maximize.
- `p99_latency_ms`: end-to-end request latency in milliseconds, minimize.

The scalar fallback/headline metric is `aggregate_throughput`.

## Correctness

The accuracy checker drives the running server over HTTP with three
reference-free gates: sentinel echo, known-answer, and greedy determinism. It
requires a real prompt-conditioned GLM-5.2 forward pass. Canned responses,
prompt echoing, skipped model execution, or non-deterministic temperature-0
decoding fail the task.
