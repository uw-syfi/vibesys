# vibe-serve-skills: overview

Serving an LLM well is almost entirely a performance-engineering problem. The model is fixed; the job is to squeeze it through the hardware at the lowest $/token and the lowest p99 latency. This file is the "read this first" guide: a short primer on the performance foundations, then a map of the skill collection organized by the questions an engineer actually asks.

## What's here

Skills are layered by abstraction — [`README.md`](README.md) has the full tree. In one line per tier:

- [`models/`](models/) — what does each model look like; what serving features it needs
- [`algorithms/`](algorithms/) — serving concepts (attention variants, async scheduling, continuous batching, paged attention, speculative decoding, ...)
- [`frameworks/`](frameworks/) — PyTorch / MLX idioms
- [`backends/`](backends/) — how to call SDPA / FlashInfer / FlashAttention / Triton / CUDA graph
- [`hardware/`](hardware/) — Hopper / Blackwell / MI300 / Apple Silicon specifics
- [`engines/`](engines/) — source-code lookup into vLLM / SGLang / TensorRT-LLM
- [`tooling/`](tooling/) — FastAPI serving, accuracy checking, benchmarking, profiling, I/O
- [`agent-gpu-skills`](https://github.com/slowlyC/agent-gpu-skills) (separate repo) — kernel implementation; out of scope here

## Scope: what this collection centers on

**The material here is primarily about serving a text-only dense-decoder LLM on NVIDIA H100-class hardware under general-purpose chat / completion traffic.** That's the default case everything is calibrated to. The collection does cover other settings — MoE (DeepSeek-V3, Qwen3-MoE), hybrid SSM (mamba-hybrid), multimodal (LLaVA-NeXT, Qwen2-VL, Whisper), Blackwell / MI300 / Apple Silicon — but those are *secondary depth*. Nearly every recipe, compatibility table, and numerical example assumes the default unless the skill explicitly addresses a non-default axis.

The foundations (roofline, prefill vs decode, batching-for-intensity, launch overhead) are universal. The recipes are not. Before applying any specific technique to a non-default setting, think about what changes along each axis:

| Axis | What changes in non-default settings |
|:-----|:-------------------------------------|
| **Model architecture** | MoE shifts pressure to dispatch + expert-weight reads and breaks the "weights-reused-across-batch" assumption. SSM / hybrid removes KV growth but changes batching semantics and radix sharing granularity. MLA compresses KV dramatically but requires specialized attention kernels. Multimodal adds encoder-side compute plus variable image / video / audio token counts that affect both scheduling and caching. |
| **Hardware** | The roofline ridge point shifts (H100 ~295 FLOP/byte ≠ H200 ≠ B200 ≠ MI300 ≠ Apple). Kernel library availability differs — FlashInfer / DeepGEMM / DeepEP are NVIDIA-only today. NVLink / Infinity-Fabric / NVL72 / none set different parallelism ceilings. Precision support differs (FP4 is Blackwell+; FP8 is Hopper+; AMD's FP8 story is newer; Apple has unified memory + INT4/INT8). |
| **Workload** | Long-context RAG stresses KV capacity and prefix-cache hit rate. High-QPS short-form stresses scheduler / launch overhead more than raw kernel speed. Agent-style branching stresses prefix sharing and concurrent decode over divergent suffixes. Multi-turn conversations shift the TTFT / TPOT weighting. Each of these pushes different levers. |

Treat this collection as a set of tools and the tradeoffs between them, **not** a recipe book. The compatibility tables in each `algorithms/*/SKILL.md` and the per-engine pointers exist precisely because the same technique applies differently across these axes. When a recipe doesn't seem to fit your setting, go back to the fundamentals in Part 1 and re-derive which bottleneck you're actually solving for.

### Principal techniques, not exhaustive

The skills here cover the **principal techniques** that show up in production serving systems today — the canonical patterns every serving engineer should know. But **this collection is not exhaustive**. New techniques appear every few months; existing ones get renamed, combined, superseded. Production engines ship with dozens of minor optimizations that don't each merit their own skill.

What the skills capture is the *pattern* and the *decision* behind each technique: what it's called, when it applies, what it trades off, how major engines implement it, and what the pitfalls look like. The exact code in vLLM / SGLang / TRT-LLM is usually more specialized than what a skill describes.

**In many cases you will need to tweak.** A technique described for dense decoder LLMs on Hopper may need a different attention kernel on Blackwell, a different cache layout on hybrid-SSM models, a different collective on AMD, a different sampler for TTS. The right question is rarely "which skill do I follow?" but "**what does this technique do that I actually need, and how do I adapt it to my setting?**"

When a skill doesn't quite fit your situation:

- Re-read the **fundamentals in Part 1** and re-derive which bottleneck you're actually solving for.
- Check the **compatibility tables** in each `algorithms/*` skill for how the axes combine.
- Look at the **engine pointers** in `engines/*` for how production systems actually implemented it — that's often the clearest source of truth.
- Use the **profiler** ([`tooling/profiler/`](tooling/profiler/)) before and after every change to confirm the technique is helping the bottleneck you think it's helping.

## Part 1 — Performance foundations

### The roofline model

Every kernel has two speed limits:

- **Compute limit**: `FLOP/s` your GPU can do.
- **Bandwidth limit**: `bytes/s` between HBM and the SMs, times your kernel's reuse.

The "arithmetic intensity" `I = FLOPs / bytes_read_from_HBM` tells you which wall you hit:

```
achievable FLOP/s = min( peak_compute,  I × peak_bandwidth )
```

The crossover `I*` (ridge point) is `peak_compute / peak_bandwidth`. For H100 BF16: `989 TFLOP/s  ÷  3.35 TB/s  ≈  295 FLOP/byte`. For H200: lower (bandwidth grew more than compute). For B200: higher again (huge FP4 compute, bandwidth grew less). **Know your ridge point.**

- `I < I*` → memory-bound: speed is set by how fast you can read from HBM.
- `I > I*` → compute-bound: speed is set by tensor-core throughput.

For LLMs the ridge point matters because the two phases of serving live on opposite sides of it.

### Prefill is compute-bound. Decode is memory-bound.

Prompt prefill of length `L` for one request, one layer:
- MatMul `X @ W`: `2 · L · H · H`  FLOPs over `L · H · 2` activation bytes + `H² · 2` weight bytes. With big `L`, weights are reused across the sequence — **high intensity, compute-bound**.
- Attention: `O(L²)` — purely compute-bound for long prompts.

Decode (one token appended, batch size `B`):
- MatMul `x @ W`: `2 · B · H²` FLOPs over `B · H · 2` act bytes + `H² · 2` weight bytes. Weights are read **once per decode step** regardless of `B`. Intensity `≈ B`.
- Attention over KV: reads the entire KV cache every step. Intensity `≈ 1` (each KV byte used in one FMA).

On H100 with `I* ≈ 295`: decode is memory-bound until batch `B ≳ 300` — far beyond what most deployments run. **Decode latency is HBM-bandwidth-limited**, not compute-limited.

### The implications everything else flows from

1. **Batching is the main lever.** Increasing `B` raises decode arithmetic intensity linearly. That's why continuous batching exists.
2. **Reducing bytes-per-step helps decode proportionally.** Quantized weights / KV cache / activations reduce HBM traffic. That's why FP8 / INT4 / KV quantization are universal.
3. **Compute-bound kernels want bigger tiles, not smaller.** Quantization doesn't help a compute-bound prefill unless you can use the quantized-precision tensor cores (FP8 on Hopper, FP4 on Blackwell).
4. **Kernel-launch overhead shows up when you're memory-bound with a small batch.** Each decode step launches dozens of kernels; at TPOT=5ms with 50 kernels at 50µs/launch, launches are 50% of the step. That's why CUDA graphs exist.
5. **MoE breaks the reuse story.** In a sparse MoE with `k` active experts per token, each token reads a different subset of expert weights. Weight reuse drops; memory pressure per token rises. That's why FP8-MoE and grouped-GEMM matter more than dense FP8.
6. **Long context breaks the KV cache.** Decode reads the KV every step — if it doesn't fit in HBM, you've lost. That's why paged attention, prefix sharing, and KV quantization exist.
7. **Multi-GPU helps memory-bound decode** (more aggregate HBM bandwidth) **and compute-bound prefill** (more FLOPs), but adds collective-communication cost. That's why the right parallelism scheme is workload-specific.

## Part 2 — Navigating by bottleneck

Diagnose first with [`tooling/profiler/`](tooling/profiler/); then read the relevant skills below.

### "Decode TPOT (per-token latency) is too slow"

The usual suspects, in order:

| Symptom | Skill | Why |
|:--------|:------|:----|
| Many small kernels, CPU-GPU gaps | [`backends/cuda-graph/`](backends/cuda-graph/) | capture the decode pass; eliminate launch overhead |
| Python scheduler / postproc gap between steps | [`algorithms/async-scheduling/`](algorithms/async-scheduling/) | run scheduler + postproc on CPU concurrently with GPU forward |
| Per-request Python sampling loop | [`algorithms/batched-sampling/`](algorithms/batched-sampling/) | one `.tolist()` instead of one `.item()` per request |
| Unfused op sequences | [`backends/flashinfer/`](backends/flashinfer/) | fused RMSNorm / RoPE / SiLU reduce intermediate writes |
| HBM bandwidth saturated, low arithmetic intensity | [`algorithms/quantization-schemes/`](algorithms/quantization-schemes/) | FP8 / INT4 weights halve / quarter byte traffic |
| KV cache larger than necessary | [`algorithms/paged-attention/`](algorithms/paged-attention/) + KV quant | smaller KV = less per-step HBM traffic |
| Decode limited by single GPU bandwidth | [`algorithms/parallelism/`](algorithms/parallelism/) | TP aggregates HBM bandwidth (cost: collectives) |
| Steps could be avoided | [`algorithms/speculative-decoding/`](algorithms/speculative-decoding/) | multiple accepted tokens per target step |

### "TTFT (time to first token) is too slow"

| Symptom | Skill | Why |
|:--------|:------|:----|
| Long prompts stalling decode | [`algorithms/chunked-prefill/`](algorithms/chunked-prefill/) | interleave prefill chunks with decode |
| Repeated prompts / branching | [`algorithms/radix-prefix-caching/`](algorithms/radix-prefix-caching/) | skip KV compute on shared prefix |
| Prefill compute-bound | [`algorithms/quantization-schemes/`](algorithms/quantization-schemes/) (FP8/FP4) + [`hardware/nvidia/`](hardware/nvidia/) | low-precision tensor cores accelerate matmul |
| Prefill too big for one GPU | [`algorithms/parallelism/`](algorithms/parallelism/) (TP) | shard weights and activations |
| Prefill competing with decode | [`algorithms/disaggregated-serving/`](algorithms/disaggregated-serving/) | dedicated prefill pool |

### "Running out of memory"

Separate the KV-capacity problem from the weight-capacity problem.

**KV cache OOM** (long context, many concurrent sessions):

| Mitigation | Skill |
|:-----------|:------|
| Paged blocks eliminate fragmentation | [`algorithms/paged-attention/`](algorithms/paged-attention/) |
| Share KV across prefix-duplicate requests | [`algorithms/radix-prefix-caching/`](algorithms/radix-prefix-caching/) |
| Quantize KV to FP8 / INT4 | [`algorithms/quantization-schemes/`](algorithms/quantization-schemes/) |
| Tier KV to CPU / disk | [`algorithms/radix-prefix-caching/`](algorithms/radix-prefix-caching/) (HiCache section) |
| SSM / hybrid model (fixed state) | [`models/ssm-hybrid/`](models/ssm-hybrid/) |
| Mixed layer types (attn + SWA / SSM / vision) need unified allocation | [`algorithms/heterogeneous-kv-cache/`](algorithms/heterogeneous-kv-cache/) |

**Weight OOM** (model doesn't fit):

| Mitigation | Skill |
|:-----------|:------|
| Weight-only INT4 (AWQ / GPTQ / Marlin) | [`algorithms/quantization-schemes/`](algorithms/quantization-schemes/) |
| Weight + activation FP8 / FP4 | same |
| Shard across GPUs | [`algorithms/parallelism/`](algorithms/parallelism/) |
| MoE sparsely activates weights | [`models/text-moe/`](models/text-moe/), [`algorithms/moe-routing-dispatch/`](algorithms/moe-routing-dispatch/) |

### "Scaling across GPUs / nodes"

| Question | Skill |
|:---------|:------|
| Which parallelism strategy? | [`algorithms/parallelism/`](algorithms/parallelism/) |
| MoE-specific dispatch | [`algorithms/moe-routing-dispatch/`](algorithms/moe-routing-dispatch/) |
| Separate prefill from decode workers | [`algorithms/disaggregated-serving/`](algorithms/disaggregated-serving/) |
| NVLink domain sizing | [`hardware/nvidia/`](hardware/nvidia/) |

Rule of thumb: stay within one NVLink domain for TP and EP; use PP or DP to cross domains.

### "Need to support a new model"

1. Read [`models/`](models/) for the closest-architecture skill to see the serving-relevant features (GQA vs MLA, dense vs MoE, RoPE variant, multimodal pipeline).
2. Find a similar model in [`engines/{vllm,sglang,trtllm}/`](engines/) — its implementation file is the template.
3. If the model has MoE / MLA / hybrid-SSM: read the corresponding [`algorithms/`](algorithms/) skill; these change the engine integration, not just the forward pass.

### "Multimodal"

| Modality | Skill |
|:---------|:------|
| Vision-language (fixed tile, dynamic tile, variable-res, cross-attn) | [`models/vision-language/`](models/vision-language/) |
| Speech in → text out (ASR, audio-LLM) | [`models/speech-language/`](models/speech-language/) |
| Text-to-speech (TTS) | [`models/speech-generation/`](models/speech-generation/) |
| Image generation (diffusion) | [`models/image-generation/`](models/image-generation/) |
| Video generation (diffusion) | [`models/video-generation/`](models/video-generation/) |
| Preprocessing (any modality) | [`tooling/io-handling/`](tooling/io-handling/) |

### "Verify correctness / benchmark"

| Goal | Skill |
|:-----|:------|
| Compare custom implementation to HF reference | [`tooling/accuracy-checker/`](tooling/accuracy-checker/) |
| Measure TTFT / TPOT / p99 correctly | [`tooling/serving-benchmark/`](tooling/serving-benchmark/) |
| Diagnose timeline / kernel bottlenecks | [`tooling/profiler/`](tooling/profiler/) |
| Serve over HTTP with OpenAI-compatible API | [`tooling/fastapi-serving/`](tooling/fastapi-serving/) |

## Part 3 — Reading orders

### (A) Build a serving engine from scratch — phased workflow

The canonical phase ordering. Each phase produces something that works; each adds a capability. Use [`tooling/profiler/`](tooling/profiler/) throughout to check that the bottleneck you're about to address is actually the current bottleneck — skip phases that aren't limiting you.

**Phase 1 — Understand the target.** Before writing anything:

- **Model architecture**: read the relevant [`models/<type>/`](models/) skill (text-dense / text-moe / ssm-hybrid / vision-language / speech-language / image-gen / video-gen / speech-gen) and [`algorithms/attention-variants/`](algorithms/attention-variants/) for the specific attention flavor (MHA / GQA / MLA / SSM / cross-attn) your model uses.
- **Workload**: chat vs RAG (long prompts, shared prefixes) vs agent (branching) vs TTS vs image-gen — these push different levers. See Part 2 above.
- **Target interface**: OpenAI-compatible API? WebSocket realtime? See [`tooling/openai-api/`](tooling/openai-api/) for the per-modality contracts.

**Phase 2 — Make it run in the target interface.** Single request, correct, exposed.

- [`tooling/fastapi-serving/`](tooling/fastapi-serving/) — wrap the model in a FastAPI endpoint conforming to the chosen API contract.
- [`tooling/accuracy-checker/`](tooling/accuracy-checker/) — verify token-level correctness against a reference (HF `model.generate()`, or the original checkpoint's sample outputs).

At the end of Phase 2 the server handles one request at a time, correctly. That's the floor.

**Phase 3 — Enable batching.** Stop wasting the GPU on single requests.

- [`algorithms/continuous-batching/`](algorithms/continuous-batching/) — requests join and leave the batch mid-step.
- [`algorithms/paged-attention/`](algorithms/paged-attention/) — block-based KV cache that makes variable-length batching efficient without padding waste.

**Phase 4 — Visit each component.** Replace naive implementations with production kernels, one component at a time.

- **Attention**: [`backends/sdpa/`](backends/sdpa/) (dependency-light baseline and single-batch fixed-shape CUDA graphs), [`backends/flashinfer/`](backends/flashinfer/) (plan/run wrappers, MLA variants, cascade for shared prefixes), or [`backends/flashattention/`](backends/flashattention/) (varlen + paged `flash_attn_with_kvcache`).
- **Non-attention fused ops** (RMSNorm, RoPE, SiLU, layer norm, fused residual): [`backends/flashinfer/`](backends/flashinfer/) fused-op section.
- **Sampling**: [`algorithms/batched-sampling/`](algorithms/batched-sampling/) — one `.tolist()` per step instead of one `.item()` per request.
- **Quantization** (if relevant): [`algorithms/quantization-schemes/`](algorithms/quantization-schemes/) — FP8 / INT4 / FP4 as the hardware allows.
- **Model-family-specific components**: MoE dispatch ([`algorithms/moe-routing-dispatch/`](algorithms/moe-routing-dispatch/)), speculative decoding ([`algorithms/speculative-decoding/`](algorithms/speculative-decoding/)), structured output ([`algorithms/structured-output/`](algorithms/structured-output/)).

**Phase 5 — Reduce CPU overhead.** Stop the GPU from waiting for the CPU.

- [`backends/cuda-graph/`](backends/cuda-graph/) — capture the decode pass; eliminate per-kernel launch overhead. Full-graph or piecewise.
- [`algorithms/async-scheduling/`](algorithms/async-scheduling/) — pipeline scheduler prep and result postproc with the GPU forward (SGLang overlap scheduler, vLLM AsyncScheduler + MRV2 patterns).
- [`frameworks/pytorch/`](frameworks/pytorch/) — sync-point catalogue, multi-stream + CUDA-event patterns, warmup discipline, static preallocation, gather-based input prep.

**Phase 6 — Benchmark and iterate.** Measure against a realistic workload.

- [`tooling/serving-benchmark/`](tooling/serving-benchmark/) — TTFT / TPOT / p99, open-loop vs closed-loop, ISL/OSL sweeps. Most benchmarks are wrong; this skill is about not reproducing that.

**Throughout — profile to decide what to do next.** [`tooling/profiler/`](tooling/profiler/) — torch profiler for Python-side, `nsys` for timeline classification, `ncu` for kernel-local metrics. Before moving to the next phase, confirm with a profile that your current bottleneck matches what the phase addresses. If kernel launches already look tight but the scheduler gaps dominate, skip Phase 5's cuda-graph step and go straight to async-scheduling. If the GPU is already saturated inside kernels, no amount of scheduler work will help — escalate to kernel-level.

### (B) Extend vLLM / SGLang / TensorRT-LLM

1. [`engines/<engine>/`](engines/) — find where the thing lives
2. [`algorithms/<topic>/`](algorithms/) — understand the concept cross-engine
3. [`models/<model>/`](models/) — if it's a model-specific change, read existing impls first
4. Read the actual source; engines move fast — skills are navigation, not replacements

### (C) Deploy at production scale

1. [`algorithms/parallelism/`](algorithms/parallelism/) — pick the scheme
2. [`algorithms/disaggregated-serving/`](algorithms/disaggregated-serving/) — if workload warrants
3. [`algorithms/quantization-schemes/`](algorithms/quantization-schemes/) — what precision makes sense for your hardware
4. [`hardware/`](hardware/) — actual capabilities of your SKU
5. [`tooling/serving-benchmark/`](tooling/serving-benchmark/) — measure at real concurrency
6. [`tooling/profiler/`](tooling/profiler/) — close the loop on regressions

### (D) Debug a slow deployment

1. [`tooling/profiler/`](tooling/profiler/) — nsys first, ncu only if kernel-bound
2. Classify bottleneck (see taxonomy in that skill)
3. Follow the bottleneck table above to the right mitigation skill
4. Verify with a repeatable benchmark

## Part 4 — Kernel boundary

Writing new CUDA / Triton / CUTLASS kernels is **out of scope** for this collection. When a skill here says "FlashInfer provides X", it means "call it from Python"; the kernel's internals live in [`agent-gpu-skills`](https://github.com/slowlyC/agent-gpu-skills).

Split of concerns:

| Concern | Here | agent-gpu-skills |
|:--------|:-----|:-----------------|
| "How do I use FlashInfer's paged-attention wrapper?" | ✓ | |
| "How does a paged-attention kernel work internally?" | | ✓ |
| "Which attention backend should vLLM use on H200?" | ✓ | |
| "How do I write a new WGMMA-based attention kernel?" | | ✓ |
| "Why is my decode memory-bound?" | ✓ | |
| "Why is my kernel's occupancy low?" | | ✓ |

## Numbers to remember (H100 baseline)

| Quantity | Value | Source |
|:---------|:------|:-------|
| BF16 tensor-core peak | 989 TFLOP/s | SM count × clock × cores × 2 |
| FP8 tensor-core peak | 1979 TFLOP/s | 2× BF16 |
| HBM3 bandwidth | 3.35 TB/s | H100 SXM |
| Ridge point (BF16 / HBM3) | ~295 FLOP/byte | compute ÷ bandwidth |
| NVLink 4 per-GPU | 900 GB/s bidirectional | 18 links × 50 GB/s |
| KV cache per token per layer | `2 × num_kv_heads × head_dim × bytes_per_elt` | exact |
| KV for Llama-3-70B, BF16, 1 token | ~40 KB (8 KV heads × 128 dim × 2B × 80 layers × 2 for K/V) | formula above |

These are starting points; [`hardware/`](hardware/) has generation-specific deltas.
