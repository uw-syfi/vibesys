# Video generation

Video-gen serving is image-gen serving × temporal dimension, which is not a small multiplier. Activation memory scales with `frames × height × width × hidden`; naive scaling of image techniques OOMs instantly. The art is in managing memory, exploiting step caching, and using 3D-VAE compression aggressively.

## Canonical pipeline

```
prompt (+ optional reference image / video) ──► text encoders (T5-XXL / LLaVA-style)
                                                       │
                                                       ▼
                                                conditioning embeddings
                                                       │
                   noise (shape (B, T_latent, C, H_latent, W_latent)) ──► DENOISER (DiT, 3D)
                                                       │  ×N steps
                                                       ▼
                                               clean video latent
                                                       │
                                                       ▼
                                            3D VAE decoder ─► pixel video (B, T, 3, H, W)
```

The denoiser is a 3D DiT; the VAE is a 3D VAE that compresses both spatially and temporally. Both are heavy.

## Attention patterns

Video DiTs use three flavors of attention:

| Pattern | Complexity | Used in |
|:--------|:-----------|:--------|
| **Spatial-only** | `O(H*W × H*W)` per frame | early open video models |
| **Temporal-only** | `O(T × T)` per pixel | weaker temporal coherence |
| **Full 3D (joint spatio-temporal)** | `O((T*H*W)²)` | Sora-class; memory-explosive but best quality |
| **Sparse 3D / window 3D** | `O(T*H*W × window)` | practical compromise |

Modern open models (HunyuanVideo, Mochi, Wan 2.1) use variants of full or near-full 3D attention on compressed latents. 3D-VAE compresses `(T, H, W)` by ~4× temporally and 8× spatially — huge memory win.

## Example architectures

### CogVideoX (5B / 2B)

- 3D DiT
- 3D VAE with 4× temporal + 8×8 spatial compression → ~192× latent-pixel ratio
- T5-XXL text encoder
- Trained for ~50 steps, DPM++ scheduler
- 720p / ~6s generation feasible on a single H100 with careful memory management

### HunyuanVideo (~13B denoiser)

- Largest open video model (as of late 2024–2025)
- 3D DiT + 3D VAE
- Very high quality; very demanding memory
- FP8 variants widely used; offload almost mandatory

### Mochi-1 (~10B)

- DiT-based with learned rotary on 3D positions
- Strong motion coherence
- Requires multi-GPU or aggressive offload for 1080p

### LTX-Video (~2B)

- Designed for fast generation (real-time-ish on 1× H100)
- DiT with lighter attention, optimized inference
- Lower quality than HunyuanVideo but much faster

### Wan 2.1 / 2.2

- Alibaba's open video family
- Multiple scales (~1.3B, ~14B)
- Text-to-video + image-to-video variants

### Open-Sora, Open-Sora-Plan

- Community open video models reproducing Sora-style behavior
- DiT backbones, 3D VAE, various step counts

## Memory reality

For a 5s 720p generation:

- Raw pixel frames: `120 × 720 × 1280 × 3 × 2` bytes (BF16) = ~660 MB (just frames)
- Latent space: maybe `30 × 90 × 160 × 16 × 2` = ~13 MB (4× temporal, 8× spatial, 16 latent channels) — much smaller, but...
- Denoiser activations per step include attention over the full latent — easily several GB
- VAE decoder: the decoder operates on uncompressed intermediate tensors; often **the memory peak of the entire pipeline**

Serving a video model without offload / tiling often needs 40-80GB VRAM. With it, 24GB is plausible for smaller models.

## Optimization techniques specific to video-gen

### Step caching (TEA-cache, DeltaDiT, etc.)

Observation: in video denoising, many DiT blocks change little between steps. Caching their outputs and reusing skips whole blocks. TEA-cache (and similar) can cut steps by 30-50% with minor quality loss.

### Chunked temporal generation

Generate frames in temporal chunks, with overlap + blending. Fixed total VRAM regardless of final video length. Tradeoff: temporal coherence at chunk boundaries.

### Tiling (spatial)

For very-high-res: denoise overlapping spatial tiles, stitch. Avoids giant activations. Mostly seen in image-gen (SDXL tiling); applies to video VAE too.

### VAE decoder tiling / CPU offload

VAE decoder is often the peak memory consumer. Common mitigations:
- Tiled VAE decode: decode chunks of frames, stitch
- CPU offload: move VAE to CPU between denoising and decode
- Decoder quantization

### CFG caching

Cache the unconditional denoiser output across steps (it changes slowly); mix with fresh conditional. Halves effective FLOPs at modest quality cost.

### FP8 / FP4 quantization

Flux-era FP8 quantization tooling (modelopt, diffusers quanto) extends to video models. HunyuanVideo FP8 is practically required for single-GPU serving.

## Serving stack

Video-gen serving is almost entirely outside the LLM-serving engine ecosystem:

- **diffusers pipelines**: canonical entry, HF-hosted video models load via `diffusers`
- **ComfyUI**: the production serving environment for open video generation, node-based pipelines
- **vLLM / SGLang / TRT-LLM**: do not serve video-gen (text encoder side only, occasionally)

Most production video-gen is served via custom pipelines over FastAPI, often built on diffusers + CUDA-graph + FP8, or via ComfyUI backends.

## Realistic latency

Video generation is not interactive. For reference (H100, single GPU, not optimized):

| Model | Resolution / length | Typical wall-clock |
|:------|:-------------------|:-------------------|
| LTX-Video | 768×512, 5s | 10-30s |
| CogVideoX-5B | 720×480, 6s | 2-5 min |
| HunyuanVideo | 720p, 5s | 5-15 min |

Expect users to wait. Serving architecture is throughput-first (batch many jobs through a worker pool), not latency-first.

## Pitfalls

- **Naive single-pass 1080p generation.** OOMs. Use tiling / chunking.
- **Treating VAE decoder as negligible.** It's often the peak memory step.
- **Text encoder on GPU during denoising.** Hold T5-XXL on CPU / offload between phases.
- **CFG-doubled batch forgotten.** Denoiser runs at effective batch `B × 2` unless CFG is 1.
- **Step caching on low-quality eval only.** TEA-cache etc. look fine in metrics but can degrade motion quality; review on long motion sequences.
- **Chunked temporal with insufficient overlap.** Seams visible at chunk boundaries; overlap 4-8 frames with blending.
- **Quantized 3D VAE decoder.** Quality loss shows up as banding / temporal flicker; some decoders don't tolerate FP8.
- **Over-aggressive step distillation.** Few-step video often loses coherence; step caching is usually safer than distillation.
- **Unclear benchmark practices.** Report wall-clock *and* memory peak *and* VAE decoder time separately.
- **Confusing image-gen tooling with video.** Many tricks (e.g., Flash diffusion, step distillation) port imperfectly.

## See also

- [`models/image-generation/`](image-generation.md) — shared diffusion foundations
- [`algorithms/quantization-schemes/`](../algorithms/quantization-schemes.md) — FP8 / FP4 for video DiTs
- [`hardware/nvidia/`](../hardware/nvidia.md) — Blackwell FP4 + NVL72 domain relevant here
- [`OVERVIEW.md`](../../OVERVIEW.md) — generative-media scope caveat
