# Image generation

Image-gen serving is a different beast from LLM serving: iterative denoising replaces autoregression, the computation is dominated by a core denoiser called N times (plus a VAE at the end), and there's no KV cache in the usual sense. Most LLM-serving techniques (paged attention, continuous batching, speculative decoding, radix cache) **don't apply**; a separate set of techniques is relevant.

## Canonical pipeline

```
prompt (text) ──► text encoder (CLIP-L / CLIP-G / T5-XXL)
                        │
                        ▼
                 text embeddings (conditioning)
                        │
                        ▼
noise ────────► DENOISER (U-Net or DiT) ×N steps ─► clean latent
                        │
                        ▼
                  VAE decoder ─► pixel image
```

Variations:
- **CFG (classifier-free guidance)**: run denoiser with conditional + unconditional embeddings and linearly combine. **Effective batch doubles.**
- **Img2Img / inpainting**: init latent isn't pure noise; mask may gate update.
- **ControlNet**: extra conditioning images (pose, depth, edge) through a parallel network.
- **IP-Adapter / reference-image conditioning**: visual embeddings added to text conditioning.
- **LoRA adapters**: low-rank modifications stacked on the denoiser at serve time.

## Two denoiser architectures

### U-Net (older, SD 1.x / 2.x / XL)

- Encoder-decoder conv net with ResBlocks + cross-attention to text embeddings
- Spatial downsampling 4×, skip connections symmetric
- Attention blocks at lower resolutions
- Size: SD 1.5 ~860M, SDXL ~2.6B denoiser + 1.1B refiner

### DiT (Diffusion Transformer, modern)

- Pure transformer over patches of the latent
- Text conditioning via cross-attention or adaLN-zero modulation
- Scales better than U-Net; dominates new models

## Example architectures

### Stable Diffusion XL

- 3-stage: text encoders (CLIP-L + CLIP-G) → 2.6B U-Net → 1.1B refiner (optional) → VAE decoder
- Latent 128×128 @ 4ch for 1024² output
- Trained for CFG scales ~6-10, 20-50 steps
- Runs well on 24GB VRAM; FP16/BF16 standard

### Stable Diffusion 3 / 3.5 (MMDiT)

- **MMDiT**: text and image tokens in the same transformer with bidirectional attention; text tokens attend to image and vice versa
- Text encoders: CLIP-L + CLIP-G + T5-XXL
- Flow-matching objective (not DDPM-style); different scheduler
- SD3.5 Large at ~8B params, VRAM-hungry

### Flux.1 (dev / schnell / pro)

- ~12B parameter DiT, T5-XXL + CLIP text encoders
- **Flow matching**, rectified-flow sampler
- Schnell: few-step distilled variant (~4 steps)
- FP8 quantization widely used (TRT-LLM ModelOpt, diffusers BitsAndBytes)
- Huge VAE decoder; VRAM pressure is real

### PixArt-Σ, Hunyuan-DiT, AuraFlow, Kolors

- Various DiT designs at different scales (0.6B to 8B+)
- Different text-encoder choices (T5-XXL, GLM, CLIP combinations)

## Step schedulers

The denoising trajectory matters as much as the model:

| Scheduler | Steps (typical) | Use |
|:----------|:---------------|:----|
| DDIM | 20-50 | general |
| Euler / Euler-A | 20-30 | general |
| DPM++ 2M Karras | 20-30 | popular SD default |
| LCM | 4-8 | distilled latent-consistency |
| Turbo / Lightning (distilled) | 1-4 | SDXL-Turbo, SDXL-Lightning |
| Flow matching (rectified) | 25-50 | SD3, Flux |
| Flow matching (distilled, e.g. Flux Schnell) | 4 | |

Distilled variants trade quality for step count (huge latency win).

## What's different from LLM serving

| Concept | Image-gen equivalent |
|:--------|:---------------------|
| Autoregressive decode | N-step iterative denoising |
| KV cache | N/A — each step starts from prior step's output, no per-token cache |
| Continuous batching | "bucket" requests with matching step count + same scheduler |
| CUDA graph | yes — each denoiser step is a fixed-shape forward, great for capture |
| Paged attention | N/A for the denoiser (images have fixed latent shape); applies only to long-context text-encoder (rarely a bottleneck) |
| Radix / prefix caching | N/A |
| Speculative decoding | N/A |
| Quantization | FP8 for Flux, SD3, modelopt; weight-only INT4 for consumer VRAM |
| CFG | doubles effective batch (conditional + unconditional) — or use CFG-distilled variants to avoid the doubling |

## Serving architecture

Unlike LLM serving, image-gen serving is usually organized per-pipeline:

```
Request queue
    ↓
Scheduler (groups by model + scheduler + step count)
    ↓
Pipeline worker (holds loaded denoiser + VAE + text encoders)
    ↓
For each bucketed batch:
    1. Encode text (batched)
    2. For step in range(N):
        denoiser forward (batched, CFG-doubled if applicable)
    3. VAE decode (often the slowest single step — consider CPU offload)
    4. Save / stream PNG
```

LoRA / ControlNet / IP-Adapter are applied at pipeline-load time; dynamic LoRA hot-swap requires engineering.

### Memory choreography

On single-GPU deployments with large models (Flux 12B), text encoders + denoiser + VAE don't all fit at once. Common pattern:

1. Load text encoders → encode → unload (or keep on CPU)
2. Load denoiser → run N steps → keep on GPU if iterating, else unload
3. Load VAE → decode → output

`diffusers` offers `enable_model_cpu_offload` / `enable_sequential_cpu_offload` to do this automatically.

## Libraries and serving stacks

| Stack | Strength |
|:------|:---------|
| **diffusers** (HF) | canonical Python library; most pipelines come with it |
| **ComfyUI** | node-based pipeline, production-deployed for customizable workflows |
| **TRT-LLM (partial)** | Flux / SD3 paths available; fastest inference on NVIDIA |
| **stable-fast / xFormers-optimized** | CUDA-graph and kernel-fusion for diffusers |
| **vLLM** | not an image-gen engine today (as of 2025–2026); text-encoder side only |
| **SGLang** | similarly text-encoder / chat-orchestration focused |

vLLM / SGLang / TRT-LLM are LLM-centric; true image-gen serving usually lives outside them.

## Quantization

| Scheme | Where | Notes |
|:-------|:------|:------|
| FP16 / BF16 | default | baseline |
| FP8 (Flux, SD3) | TRT-LLM ModelOpt, diffusers `optimum-quanto` | ~1.5-2× faster on Hopper+ |
| INT8 / INT4 | consumer memory savings | quality risk |
| NVFP4 / MXFP4 | Blackwell | newest |

CFG combined with FP8 is standard on production Flux serving.

## Pitfalls

- **Treating image-gen as LLM.** KV cache, paged attention, speculative decoding don't apply. Don't try to force-fit an LLM engine.
- **Forgetting CFG doubles batch.** A batch of 4 images with CFG is 8 forwards per step internally.
- **Wrong scheduler for model.** Flow-matching models (SD3, Flux) must use flow-matching schedulers; plugging a DDIM scheduler produces garbage.
- **VAE on GPU stealing denoiser VRAM.** VAE decoder can be 300M-1B+ params; can offload to CPU or another GPU.
- **LoRA merge at load vs runtime.** Merging at load is fast but baked; runtime LoRA lets you swap — pick deliberately.
- **Text-encoder quant.** T5-XXL is big; quantizing it is a real memory win but hits prompt-following quality.
- **Seed reproducibility across kernels.** Different attention backends produce different noise-schedule behavior despite same seed.
- **CUDA-graph with dynamic batch size.** Each bucket needs its own capture — don't re-capture every request.
- **Image dimensions not divisible by 8 / 16.** VAE and denoiser assume fixed divisibility; odd resolutions crash or get silently rounded.
- **CFG = 1.0.** CFG scale 1 disables guidance — the uncond path is wasted. Skip the uncond forward entirely when CFG == 1.

## See also

- [`models/video-generation/`](video-generation.md) — closely related, adds temporal dim and much higher memory
- [`models/omni-multimodal/`](omni-multimodal.md) — when image generation is one head of a unified text + image model (BAGEL, Hunyuan-Image3, MammothModa2)
- [`algorithms/quantization-schemes/`](../algorithms/quantization-schemes.md) — FP8 for Flux / SD3
- [`backends/cuda-graph/`](../backends/cuda-graph.md) — denoiser steps are graph-friendly
- [`OVERVIEW.md`](../../OVERVIEW.md) — the scope note specifically calls out generative media as a non-default setting
