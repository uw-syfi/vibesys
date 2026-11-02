# Flint INT4 Q4_1 AgentX serving on one AMD MI210

Build and optimize a serving engine for the official `RecursiveSI/flint-int4-gguf`
checkpoint on **one AMD MI210, 64 GB HBM**. The project supplies no candidate
engine or validated AMD baseline. Implementing and optimizing the Q4_1 loader,
quantized expert kernels and Flint attention on gfx90a is the task. Candidate
code lives outside the trusted `.vibesys/` tree.

## Exact quantized model

Use `flint-int4-q4_1.gguf` at the revision in `reference/meta.json`, SHA256
`43c07baccdba42eb7528a9630ee15699b1942c297291dc1b39a061f508ee1a4b`.
The file is 25,992,966,976 bytes. Its Q4_1 codes preserve the MLX export's
trained affine INT4 values, with FP16 scales and offsets; embeddings and other
floating tensors are F32. This is a fixed quantized checkpoint, not permission
to invent a new quantization recipe. Lossless repacking and fused tile-wise
dequantization are allowed. Preserve decoded values, scales, offsets, all
experts and all other trained tensors. Do not requantize, prune, substitute the
BF16 checkpoint, or change model semantics to improve a reading.

Preserve Flint's six-of-256 sigmoid expert routing plus shared expert,
per-head normalization, learned attention sinks, 35 local-attention layers
(window 513) and 14 full-attention layers without RoPE. Retain the native
262,144-token context capacity and GGUF tokenizer/chat-template semantics.
The benchmark's pinned Hugging Face tokenizer is the matching base tokenizer;
the served model identifier is the quantized repository.

Keep compressed weights on the allocated MI210 and dequantize tiles as needed.
Do not expand the whole model into BF16 device weights. CPU cache offload is
allowed within the allocation, with its costs inside measurement. Do not use
an extra GPU, another machine or a hosted server. Weights fitting HBM do not
imply four maximum-length sessions and all workspace fit: manage the cache.
Use upstream implementations as references, but own the candidate serving path.

## Quantized correctness gate

The trusted accuracy command downloads and SHA256-verifies the exact GGUF,
downloads and verifies a pinned llama.cpp source archive, builds a CPU-only
`llama-server`, and serves the reference on a private Unix socket. No GPU
memory is reserved for the reference. It is not compared against BF16 weights.

Provide a C++17 compiler, CMake, OpenSSL development headers, Python dependencies
from the project, network access or prefilled HF caches, and at least 40 GiB
available host RAM for the CPU reference plus candidate host memory. Allow
8 GiB build memory and 5 GiB temporary build space. Source builds use two jobs
and run per invocation; the one-hour accuracy deadline includes setup. Both
build/runtime and numerical tolerances remain uncalibrated on a full-size AMD
run; a deadline failure is an error, not a correctness fallback.

The gate checks eight-token greedy continuations at prompt lengths
1, 16, 32, 512, 513, 514 and 1024, with fresh content and repeatability probes.
It scores the candidate's own prefixes against CPU top-32 next-token logprobs.
A token outside that set fails closed. The shared numerical policy permits
near ties within 0.05 log probability, per-token logprob error up to 0.15 and
mean error up to 0.05. A near tie outside the returned top-32 still fails.
It also compares chat output with the GGUF template's raw completion.

Expose the same candidate HTTP interfaces as the BF16 tasks: streamed chat
with usage and reasoning/tool-history preservation; raw token-ID completions
with `return_token_ids`, logprobs and exact usage; and nonstream chat with raw
generated token IDs and standard chat logprobs. See the project README.
A passing short numerical gate does not replace long-context validation.

## AgentX objectives

Use the trusted `agentx-v1` full recipe: date-pinned June 21 coding corpus,
four concurrent session trees, seed 42, trajectory start ratios 0.25–0.75,
ten warmup requests per lane and 900 seconds of profiling. Preserve recorded
pacing, output lengths, streaming and first-turn cache busting. Smoke is
unscored and cannot produce accepted measurements.

Optimize output tokens/s (maximize), TTFT p50/p90 and TPOT p50/p90 in
milliseconds (minimize), with 3% relative noise tolerance. P90 request latency
is diagnostic. Keep this quantized model's frontier separate from BF16 tasks.
Failed requests, altered weights, shorter outputs or a reduced context cap
are not improvements. No live MI210 throughput result is supplied.
