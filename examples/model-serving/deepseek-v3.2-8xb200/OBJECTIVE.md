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

Run the benchmark command configured by `vibesys.input.toml`. The evaluator's
versioned fixed-text entrypoint may pass a different `--url`:

```bash
python3 <EVALUATOR_PACKAGE>/fixed_text.py --request-factory-engine <RF_ENGINE> \
  --model deepseek-ai/DeepSeek-V3.2 --tokenizer deepseek-ai/DeepSeek-V3.2 \
  --tokenizer-revision a7e62ac04ecb2c0a54d736dc46601c5606cf10a6 \
  --request-count 256 \
  --input-tokens 8192 --output-tokens 1024 --concurrency 64 --url <SERVER_URL>
```

Default load:

- `/v1/completions`
- streaming responses
- 256 independent requests at saturated concurrency 64
- long synthetic prompts (8192 token IDs), with no shared prefix
- `max_tokens = 1024`
- `temperature = 0`

This benchmark stresses the FP8 MoE dispatch path, expert-parallel all-to-all
communication, MLA/DSA long-context attention and KV-cache management, and
decode throughput under concurrency. Candidates must not reduce request count,
prompt length, concurrency, or max output tokens to improve the score, and must
not collapse requests into a shared cached prefix. The old 120-second rolling
window is replaced with a fixed RF trace, so scores are not directly comparable.

## Metrics

Pareto axes:

- `output_token_throughput_per_s`: RF-measured output token IDs per second,
  maximize.
- `p90_latency_ms`: RF end-to-end request latency in milliseconds, minimize.

The scalar fallback/headline metric is `output_token_throughput_per_s`.

## Correctness

The accuracy checker drives the running server over HTTP with three
reference-free gates: sentinel echo, known-answer, and greedy determinism. It
requires a real prompt-conditioned DeepSeek-V3.2 forward pass. Canned
responses, prompt echoing, skipped model execution, or non-deterministic
temperature-0 decoding fail the task.
