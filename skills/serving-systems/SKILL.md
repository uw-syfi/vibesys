---
name: serving-systems
description: >-
  LLM and multimodal serving-system development. Activate when a task
  touches inference servers, latency / throughput / TTFT / TPOT, KV-cache,
  batching, attention kernels, CUDA graphs, speculative decoding, structured
  / grammar-constrained output, quantization, MoE serving, prefix caching,
  multi-modal (vision/speech/image/video) serving, or porting a model to
  vLLM / SGLang / TensorRT-LLM.
---

# serving-systems

This skill bundles the curated reference material for **LLM and multimodal serving-system development** as a flat library of topic-focused notes under [`references/`](references/). Open the specific reference whose topic matches the task; do not preload everything.

## How to use this skill

1. Read this file once to learn what's covered.
2. For the active task, identify the **one or two topics** that match it (use the index below).
3. Open `references/<topic>.md` directly with your file-read tool. Each is self-contained.
4. Some topics have follow-up files named `references/<topic>-<sub>.md` (e.g. `cuda-graph-runner.md`); the main `<topic>.md` says when to read them.

## Default-on optimizations (NVIDIA serving)

Three techniques every production serving system on NVIDIA ships with — confirm all three are in place before pursuing workload-specific optimizations:

1. **Continuous batching** — see [`references/algorithms/continuous-batching.md`](references/algorithms/continuous-batching.md). Skip only when the workload is single-batch by contract.
2. **Fused attention kernel** — FlashInfer, FlashAttention, or SDPA. For the picker (workload → backend, plus the per-feature matrix), open [`references/backends/attention-backend-comparison.md`](references/backends/attention-backend-comparison.md); for deep usage, [`references/backends/flashinfer.md`](references/backends/flashinfer.md), [`references/backends/flashattention.md`](references/backends/flashattention.md), or [`references/backends/sdpa.md`](references/backends/sdpa.md). On NVIDIA, never skip a fused kernel.
3. **CUDA graphs** — see [`references/backends/cuda-graph.md`](references/backends/cuda-graph.md). Required for low-latency single-batch and high-throughput batched serving.

## Reference index

Each entry below is one file under [`references/`](references/). The bracketed phrase shows what triggers it.

### Model architectures

- [`references/models/image-generation.md`](references/models/image-generation.md) — Image generation serving — diffusion (U-Net, DiT) and flow-matching.

- [`references/models/omni-multimodal.md`](references/models/omni-multimodal.md) — Omni-modal serving — multi-modality in AND out.

- [`references/models/speech-generation.md`](references/models/speech-generation.md) — Speech generation serving — TTS and speech-to-speech.

- [`references/models/speech-language.md`](references/models/speech-language.md) — Speech-language serving — audio understanding (ASR, speech translation, audio-text chat).

- [`references/models/ssm-hybrid.md`](references/models/ssm-hybrid.md) — State-space and hybrid SSM+attention serving — Mamba/Mamba2, Jamba, Zamba/Zamba2, Nemotron-H (dense and hybrid), Jet-Nemotron, Falcon-Mamba.

- [`references/models/text-dense.md`](references/models/text-dense.md) — The foundational architecture most modern LLMs build on.

- [`references/models/text-moe.md`](references/models/text-moe.md) — Mixture-of-Experts text decoders for serving — coarse-grained MoE (Mixtral 8x7B, 8x22B) to fine-grained MoE with shared experts (DeepSeek V2/V3/R1, Qwen3-MoE 30B-A3B / 235B-A22B, Llama-4 Scout/Maverick) and DeepSeek's MLA+MoE+MTP stack.

- [`references/models/video-generation.md`](references/models/video-generation.md) — Video generation serving — diffusion-based with 3D attention, temporal+spatial denoising, large activations.

- [`references/models/vision-language.md`](references/models/vision-language.md) — Vision-language serving — LLaVA / LLaVA-NeXT / LLaVA-OneVision (fixed and dynamic tiling), Qwen-VL / 2-VL / 2.5-VL / 3-VL (native-resolution ViT + M-RoPE), InternVL, mllama (Llama-3.2 Vision, cross-attention), Molmo, DeepSeek-VL.


### Serving algorithms

- [`references/algorithms/async-scheduling.md`](references/algorithms/async-scheduling.md) — Hide CPU scheduler / model-runner / Python overhead behind the GPU forward.

- [`references/algorithms/attention-variants.md`](references/algorithms/attention-variants.md) — Attention variants in modern serving — three orthogonal axes: head sharing (MHA / MQA / GQA / MLA), masking pattern (causal / bidirectional / sliding-window / cross-attention / 3D / tree), complexity class (quadratic vs SSM / linear / RetNet / RWKV / hybrid).

- [`references/algorithms/batched-sampling.md`](references/algorithms/batched-sampling.md) — Efficient batched sampling on GPU for LLM serving — temperature, top-p, top-k, min-p, repetition / presence / frequency penalties, typical, rejection sampling for combined filters — without per-request CPU-GPU sync.

- [`references/algorithms/chunked-prefill.md`](references/algorithms/chunked-prefill.md) — Chunked prefill scheduling — split long prompts into smaller token chunks interleaved with decode requests in a single forward pass, preventing a long prefill from stalling decode latency.

- [`references/algorithms/continuous-batching.md`](references/algorithms/continuous-batching.md) — Implement continuous batching for an LLM inference server.

- [`references/algorithms/disaggregated-serving.md`](references/algorithms/disaggregated-serving.md) — Disaggregated prefill / decode (P/D) serving — separate worker pools for prefill and decode stages, with KV cache transferred between them.

- [`references/algorithms/heterogeneous-kv-cache.md`](references/algorithms/heterogeneous-kv-cache.md) — Memory management and prefix caching for hybrid models (full-attn + sliding-window, attention + SSM/Mamba, attention + linear).

- [`references/algorithms/moe-routing-dispatch.md`](references/algorithms/moe-routing-dispatch.md) — MoE routing and dispatch for serving — top-k gating (softmax, group-limited, auxiliary-loss-free), token-to-expert dispatch/combine (padding, permutation, DeepEP all-to-all), grouped-GEMM expert FFN, Marlin-MoE / CUTLASS-MoE kernels, expert parallelism (EP), expert load balancing (EPLB), shared experts.

- [`references/algorithms/paged-attention.md`](references/algorithms/paged-attention.md) — Paged KV cache design for LLM serving — block-based non-contiguous KV storage with a page table per request, allowing dynamic growth without fragmentation.

- [`references/algorithms/parallelism.md`](references/algorithms/parallelism.md) — Parallelism strategies for LLM serving — TP, PP, EP, DP, SP, and combos (TP+EP, DP-attention + EP-MoE, TP+SP, TP+PP, context parallel).

- [`references/algorithms/quantization-schemes.md`](references/algorithms/quantization-schemes.md) — Quantization schemes for LLM serving — FP8 (E4M3/E5M2, per-tensor vs per-channel vs block), INT4 weight-only (AWQ, GPTQ, Marlin, GGUF, petit), INT8, FP4 (MXFP4, NVFP4), FP quant mixed-precision (qutlass / modelopt), weight-only vs.

- [`references/algorithms/radix-prefix-caching.md`](references/algorithms/radix-prefix-caching.md) — Prefix / radix KV cache reuse — share KV cache pages across requests with common prefixes via a radix tree, LRU eviction.

- [`references/algorithms/speculative-decoding.md`](references/algorithms/speculative-decoding.md) — Speculative decoding for LLM serving — draft proposals verified in one target pass.

- [`references/algorithms/structured-output.md`](references/algorithms/structured-output.md) — Structured output at serving time — grammar-guided decoding (XGrammar, Outlines, llguidance, lm-format-enforcer), JSON mode, regex-constrained output, CFG / pushdown grammars, tool / function calling, logits biasing.


### Kernel-library backends

- [`references/backends/attention-backend-comparison.md`](references/backends/attention-backend-comparison.md) — Picking among FlashInfer, FlashAttention, and SDPA — workload-to-backend table, per-feature matrix, plan/run cost on single-batch latency, hardware support, migration paths, common pitfalls.

- [`references/backends/cuda-graph.md`](references/backends/cuda-graph.md) — CUDA graph capture/replay for serving — eliminate per-kernel CPU launch overhead.

- [`references/backends/flashattention.md`](references/backends/flashattention.md) — Integrate FlashAttention into a serving engine with explicit KV-cache management.

- [`references/backends/flashinfer.md`](references/backends/flashinfer.md) — FlashInfer library usage in serving engines.

- [`references/backends/sdpa.md`](references/backends/sdpa.md) — Use PyTorch scaled dot product attention (SDPA) as a serving attention backend.

- [`references/backends/triton-kernels.md`](references/backends/triton-kernels.md) — Consuming existing Triton kernels in serving engines — invocation patterns, autotune caching, compile-time constants, warmup, and interaction with CUDA graphs and torch.compile.


### Frameworks (PyTorch / Triton / MLX)

- [`references/frameworks/mlx.md`](references/frameworks/mlx.md) — MLX framework for LLM serving on Apple Silicon — unified-memory array model, lazy evaluation and `mx.eval`, `mx.compile`, `mx.fast` kernels, mlx-lm reference serving path, native INT4/INT8 quantization via `mx.quantize`, custom Metal kernels via `mx.fast.metal_kernel`.

- [`references/frameworks/pytorch.md`](references/frameworks/pytorch.md) — PyTorch idioms for LLM serving — nn.Module + weight loading, torch.compile (Dynamo / inductor / dynamic shapes / reduce-overhead), state_dict remapping, custom op registration with torch.library, NCCL setup, HF transformers integration, inference_mode.

- [`references/frameworks/triton.md`](references/frameworks/triton.md) — Triton as a framework-level decision — when a custom Triton kernel pays off in serving vs reusing FlashInfer / FlashAttention / CUTLASS / liger / sgl-kernel.


### Hardware specifics

- [`references/hardware/amd-mi300.md`](references/hardware/amd-mi300.md) — AMD Instinct MI300-family hardware specs for serving — MI300X, MI325X, MI350X.

- [`references/hardware/apple-silicon.md`](references/hardware/apple-silicon.md) — Apple Silicon (M-series SoC) hardware specs for serving — M1 / M2 / M3 / M4 (and Pro / Max / Ultra variants).

- [`references/hardware/nvidia.md`](references/hardware/nvidia.md) — NVIDIA data-center / workstation GPU specs across 5 generations: Blackwell (B200, RTX PRO 6000), Hopper (H200, H100), Ada (L40S, L4), Ampere (A100 40/80, A10), Turing (T4).


### Engine source maps

- [`references/engines/sglang.md`](references/engines/sglang.md) — SGLang serving engine source-code lookup — SRT runtime, scheduler/tokenizer managers, attention backends (FlashInfer, FlashInfer-MLA, CUTLASS-MLA, FA, FlashMLA, Triton), MoE routing/dispatch + EPLB, radix cache + HiCache, quantization, speculative decoding (EAGLE), disaggregated P/D, TP/PP/EP, CUDA graphs, sgl-kernel custom kernels.

- [`references/engines/trtllm.md`](references/engines/trtllm.md) — TensorRT-LLM serving engine source-code lookup.

- [`references/engines/vllm.md`](references/engines/vllm.md) — vLLM serving engine source-code lookup.


### API / benchmark / profiler tooling

- [`references/tooling/accuracy-checker.md`](references/tooling/accuracy-checker.md) — Create an accuracy verification script that compares a custom generation implementation against HuggingFace model.generate() as the reference.

- [`references/tooling/fastapi-serving.md`](references/tooling/fastapi-serving.md) — Create a production-ready FastAPI inference server for HuggingFace transformer models.

- [`references/tooling/io-handling.md`](references/tooling/io-handling.md) — Request-time I/O handling for LLM/multimodal serving — tokenization and chat templates, tool-call prompt formatting, image preprocessing (resize / tile / normalize / patchify), video frame sampling, audio feature extraction (log-mel), detokenization and UTF-8-safe streaming, tool-call parsing per model family, structured-output extraction.

- [`references/tooling/lora-serving.md`](references/tooling/lora-serving.md) — Multi-adapter LoRA serving — single base model dispatching different LoRA adapters per request at serving throughput.

- [`references/tooling/openai-api.md`](references/tooling/openai-api.md) — OpenAI-compatible HTTP per modality — text (`/v1/completions`, `/v1/chat/completions`), image (`/v1/images/generations`), TTS (`/v1/audio/speech`), STT (`/v1/audio/transcriptions`), video (`/v1/videos` + async polling), realtime audio (WS `/v1/realtime`).

- [`references/tooling/profiler.md`](references/tooling/profiler.md) — GPU performance profilers for LLM/multimodal serving: PyTorch Profiler (Python-level op aggregation, Chrome trace), Nsight Systems (`nsys`, system-timeline / launch-gap / NCCL diagnosis), Nsight Compute (`ncu`, kernel metrics / occupancy / roofline).

- [`references/tooling/serving-benchmark.md`](references/tooling/serving-benchmark.md) — Benchmark an LLM serving endpoint — TTFT, TPOT, ITL, end-to-end latency, throughput, p50/p95/p99 across concurrency and ISL/OSL sweeps.


## Out of scope

Kernel implementation (writing CUDA / Triton / CUTLASS). For that, use the separate `agent-gpu-skills` collection.

## Reference repos

The `repos/` directory (excluded from materialization to agents) holds full source trees of vLLM, SGLang, and TensorRT-LLM as git submodules. Engine-source-map references in this skill cite paths like `$SERVE_REPOS/<engine>/...`; export `SERVE_REPOS=$(git rev-parse --show-toplevel)/skills/serving-systems/repos` or substitute inline.
