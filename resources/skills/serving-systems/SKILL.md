---
name: serving-systems
description: >-
  LLM and multimodal serving systems. Activate on inference servers,
  latency / throughput / TTFT / TPOT, KV-cache, batching, attention kernels,
  graph capture, speculative decoding, structured output, quantization, MoE,
  prefix caching, vision/speech/image/video serving, porting a model to
  vLLM / SGLang / TensorRT-LLM, or serving on NVIDIA, AMD ROCm,
  Apple Silicon (MLX), or Trainium (Neuron, NKI).
---

# serving-systems

This skill bundles the curated reference material for **LLM and multimodal serving-system development** as a topic library under [`references/`](references/). Open the specific reference whose topic matches the task; do not preload everything.

## How to use this skill

1. Read this file once to learn what's covered.
2. **Open [`references/platforms/`](references/platforms/) first.** Exactly one backend's directory is present — the one this run targets. Its `floor.md` is the optimization floor for your hardware.
3. For the active task, identify the **one or two topics** that match it (use the index below).
4. Open `references/<tier>/<topic>.md` directly with your file-read tool. Each is self-contained.

## Start here: your platform's floor

The default-on optimizations are **not the same across hardware**, and applying one platform's floor to another produces wrong work — eliminating padding is correct on NVIDIA and inverted on Trainium; graph capture is required on NVIDIA and does not exist on Apple Silicon.

**Open `references/platforms/<backend>/floor.md` for the backend present in this workspace.** Only that platform's directory is materialized, so there is no ambiguity about which applies.

## Portable contracts vs platform implementations

Topics split into two kinds, and the distinction is load-bearing:

- **Contracts** (`algorithms/`, `models/`, `tooling/`, `frameworks/`) state the problem, the invariants any implementation must satisfy, and the failure modes. These are the same on every backend.
- **Implementations** (`platforms/<backend>/`) give the technique for specific hardware.

Where a contract has a platform implementation, the contract links to it. Read the contract first — it tells you what *must* be true; the platform file tells you how to get there here.

## Reference index

Each entry is one file under [`references/`](references/). The bracketed phrase shows what triggers it.

### Platforms

One directory per compute backend, each with `floor.md`, `hardware.md`, and `profiler.md` plus its own kernel and framework notes. Only the selected backend's directory is present.

- [`references/platforms/`](references/platforms/) — start at `floor.md`.

### Serving algorithms (portable contracts)

- [`references/algorithms/async-scheduling.md`](references/algorithms/async-scheduling.md) — Hide host scheduler work behind accelerator compute. Contract; mechanism is per-platform.

- [`references/algorithms/batched-sampling.md`](references/algorithms/batched-sampling.md) — Per-request sampling parameters in one kernel pipeline, without per-request host sync.

- [`references/algorithms/chunked-prefill.md`](references/algorithms/chunked-prefill.md) — Split long prompts into chunks interleaved with decode, preventing a long prefill from stalling decode latency.

- [`references/algorithms/continuous-batching.md`](references/algorithms/continuous-batching.md) — Requests join a running generation loop between steps. Contract; the KV strategy **inverts** between backends.

- [`references/algorithms/cross-attention-kv-cache.md`](references/algorithms/cross-attention-kv-cache.md) — Cross-attention KV cache for encoder-decoder decode (Whisper, mllama): compute encoder-context K/V once at prefill, read every step. Non-causal, no RoPE, separate pool.

- [`references/algorithms/disaggregated-serving.md`](references/algorithms/disaggregated-serving.md) — Separate prefill and decode worker pools with KV transfer between them.

- [`references/algorithms/heterogeneous-kv-cache.md`](references/algorithms/heterogeneous-kv-cache.md) — Memory management and prefix caching for hybrid models (full-attn + sliding-window, attention + SSM/Mamba, attention + linear).

- [`references/algorithms/moe-routing-dispatch.md`](references/algorithms/moe-routing-dispatch.md) — MoE routing and dispatch — top-k gating, token-to-expert dispatch/combine, grouped-GEMM expert FFN, expert parallelism, expert load balancing.

- [`references/algorithms/paged-attention.md`](references/algorithms/paged-attention.md) — Block-based non-contiguous KV storage with a page table per request. Applies where the backend has a discrete memory pool.

- [`references/algorithms/parallelism.md`](references/algorithms/parallelism.md) — TP, PP, EP, DP, SP and combinations. Multi-device backends only.

- [`references/algorithms/quantization-schemes.md`](references/algorithms/quantization-schemes.md) — Precision, granularity, calibration, checkpoint layout. Hardware support is generation-gated.

- [`references/algorithms/radix-prefix-caching.md`](references/algorithms/radix-prefix-caching.md) — Share KV cache across requests with common prefixes via a radix tree with LRU eviction.

- [`references/algorithms/speculative-decoding.md`](references/algorithms/speculative-decoding.md) — Draft proposals verified in one target pass. Contract; variable accept length is handled per-platform.

- [`references/algorithms/structured-output.md`](references/algorithms/structured-output.md) — Grammar-guided decoding (XGrammar, Outlines, llguidance), JSON mode, regex constraints, tool calling, logits biasing.

### Model architectures

- [`references/models/attention-variants.md`](references/models/attention-variants.md) — Attention variants across three axes: head sharing (MHA / MQA / GQA / MLA), masking pattern, complexity class.

- [`references/models/image-generation.md`](references/models/image-generation.md) — Image generation serving — diffusion (U-Net, DiT) and flow-matching.

- [`references/models/omni-multimodal.md`](references/models/omni-multimodal.md) — Omni-modal serving — multi-modality in AND out.

- [`references/models/qwen3-5.md`](references/models/qwen3-5.md): Qwen3.5-397B-A17B, hybrid Gated DeltaNet + full-attention MoE, structure and serving numbers.

- [`references/models/speech-generation.md`](references/models/speech-generation.md) — Speech generation serving — TTS and speech-to-speech.

- [`references/models/speech-language.md`](references/models/speech-language.md) — Speech-language serving — ASR, speech translation, audio-text chat.

- [`references/models/ssm-hybrid.md`](references/models/ssm-hybrid.md) — State-space and hybrid SSM+attention serving — Mamba/Mamba2, Jamba, Zamba, Nemotron-H, Falcon-Mamba.

- [`references/models/text-dense.md`](references/models/text-dense.md) — The foundational architecture most modern LLMs build on.

- [`references/models/text-moe.md`](references/models/text-moe.md) — Mixture-of-Experts text decoders — Mixtral, DeepSeek V2/V3/R1, Qwen3-MoE, Llama-4.

- [`references/models/video-generation.md`](references/models/video-generation.md) — Video generation serving — diffusion with 3D attention, large activations.

- [`references/models/vision-language.md`](references/models/vision-language.md) — Vision-language serving — LLaVA, Qwen-VL, InternVL, mllama, Molmo, DeepSeek-VL.

### Frameworks (cross-platform)

- [`references/frameworks/pytorch.md`](references/frameworks/pytorch.md) — PyTorch idioms for serving — weight loading, torch.compile, state_dict remapping, custom ops, inference_mode.

- [`references/frameworks/triton.md`](references/frameworks/triton.md) — Triton as a framework-level decision — when a custom Triton kernel pays off vs reusing an existing kernel library.

Platform-specific frameworks (MLX, torch-neuronx, NxD) live under that platform's directory.

### Engine source maps

Written against NVIDIA-first upstream trees; ROCm paths exist in vLLM and SGLang but are not the primary codepath.

- [`references/engines/sglang.md`](references/engines/sglang.md) — SGLang source-code lookup.
- [`references/engines/trtllm.md`](references/engines/trtllm.md) — TensorRT-LLM source-code lookup.
- [`references/engines/vllm.md`](references/engines/vllm.md) — vLLM source-code lookup.

### API / benchmark / profiler tooling

- [`references/tooling/accuracy-checker.md`](references/tooling/accuracy-checker.md) — Verify a custom generation implementation against HuggingFace `model.generate()`.

- [`references/tooling/fastapi-serving.md`](references/tooling/fastapi-serving.md) — Production-ready FastAPI inference server for HuggingFace models.

- [`references/tooling/io-handling.md`](references/tooling/io-handling.md) — Tokenization and chat templates, image/video/audio preprocessing, detokenization and UTF-8-safe streaming, tool-call parsing.

- [`references/tooling/lora-serving.md`](references/tooling/lora-serving.md) — Multi-adapter LoRA serving — one base model dispatching different adapters per request.

- [`references/tooling/openai-api.md`](references/tooling/openai-api.md) — OpenAI-compatible HTTP per modality — text, image, TTS, STT, video, realtime audio.

- [`references/tooling/performance-modeling.md`](references/tooling/performance-modeling.md) — Analytical serving-performance modeling — roofline, Amdahl bounds, end-to-end time accounting, architecture ceilings, profiler calibration, and plateau-driven hypothesis selection.

- [`references/tooling/profiler.md`](references/tooling/profiler.md) — Profiling discipline and altitudes. The contract is portable; the concrete toolchain is per-platform.

- [`references/tooling/serving-benchmark.md`](references/tooling/serving-benchmark.md) — Benchmark an LLM serving endpoint — TTFT, TPOT, ITL, end-to-end latency, throughput, p50/p95/p99 across concurrency and ISL/OSL sweeps.

## Out of scope

Kernel implementation (writing CUDA / Triton / CUTLASS / HIP). For that, use the separate `agent-gpu-skills` collection.

**Exception — NKI:** writing NeuronCore kernels for AWS Trainium *is* in scope here, via the bundled **`neuron-nki-*`** skills (`neuron-nki-writing`, `-docs`, `-debugging`, `-profiling`, `-profile-querying`); there is no separate Trainium kernel collection.

## Reference repos

The `repos/` directory (excluded from materialization to agents) holds full source trees of vLLM, SGLang, and TensorRT-LLM as git submodules. Engine-source-map references cite paths like `$SERVE_REPOS/<engine>/...`; export `SERVE_REPOS=$(git rev-parse --show-toplevel)/resources/skills/serving-systems/repos` or substitute inline.
