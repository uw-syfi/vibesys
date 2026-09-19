# Objective - DeepSeek-V3.2 From-Scratch 8xB200 Serving

Build an OpenAI-compatible serving system for `deepseek-ai/DeepSeek-V3.2` from
scratch across 8x NVIDIA B200 GPUs (192 GB each, 1536 GB total). No serving
engine is provided: unlike the `-sglang` variant, there is no vLLM or SGLang
checkout. `torch` and `transformers` are available as
utilities (weight loading, tokenizer, reference ops), not as a serving engine.

DeepSeek-V3.2 is a 685B-parameter MoE model (256 experts, ~37B active per
token) shipped in native FP8 (E4M3). Attention combines Multi-head Latent
Attention (MLA) with DeepSeek Sparse Attention (DSA), a learned sparse-attention
indexer that reduces long-context attention cost. At FP8 the weights (~685 GB)
fit across 8xB200 with large headroom for the MLA latent KV cache, the DSA
indexer state, and long-context batches, so the bottleneck is not raw capacity
but the execution path. The candidate implements the whole stack: weight
loading and sharding across all 8 GPUs (tensor/expert/pipeline parallelism or
a hybrid), the MLA latent-KV attention and the DSA sparse-attention indexer,
FP8 grouped-GEMM MoE experts and dequant (DeepGEMM-style on Blackwell), expert
routing/top-k dispatch and all-to-all communication across 256 experts, a
request scheduler, and the OpenAI-compatible HTTP server. This is the hardest
from-scratch target in this suite.

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

This benchmark stresses the FP8 MoE dispatch path, expert-parallel all-to-all
communication, MLA/DSA long-context attention and KV-cache management, and
decode throughput under concurrency. Candidates must not reduce prompt length,
duration, concurrency, or max output tokens to improve the score, and must not
collapse the distinct per-request prompts into a single cached prefix.

## Metrics

Pareto axes:

- `aggregate_throughput`: output tokens per second, maximize.
- `p99_latency_ms`: end-to-end request latency in milliseconds, minimize.

The scalar fallback/headline metric is `aggregate_throughput`.

## Correctness

The accuracy checker drives the running server over HTTP with three
reference-free gates: sentinel echo, known-answer, and greedy determinism. It
requires a real prompt-conditioned DeepSeek-V3.2 forward pass. Canned
responses, prompt echoing, skipped model execution, or non-deterministic
temperature-0 decoding fail the task.
