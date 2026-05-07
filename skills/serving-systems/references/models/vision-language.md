# Vision-language models

Image / video + text serving. Architecturally two separable pieces — a vision encoder and a text decoder LLM — joined in one of two ways. Operationally, the complexity is in request preprocessing, token splicing, and position-ID bookkeeping.

## Two architectural families

### (A) Token-splicing via projector

```
image ─► preprocess ─► vision encoder (CLIP / SigLIP / custom ViT)
                            │
                            ▼
                        projector (MLP) ─► image tokens [T × H_llm]
                            │
text tokens with <image> placeholders ──► SPLICE ──► LLM decoder ─► output
```

The image is encoded to a fixed or variable number of "image tokens" at the LLM's hidden size, then spliced into the text sequence where `<image>` placeholders were.

**Most VLMs follow this pattern**: LLaVA-family, Qwen-VL-family, InternVL, DeepSeek-VL, Molmo.

### (B) Cross-attention in the decoder

```
image ─► preprocess ─► vision encoder ─► image features
                                              │
                                              │ (used as K,V for cross-attn)
text tokens ────────────────────────► LLM decoder with cross-attention
                                               │
                                               ▼
                                            output
```

The decoder has extra cross-attention layers that attend to the vision features; the text sequence itself doesn't include image tokens.

**Used by**: Llama-3.2 Vision (mllama), Flamingo-style models.

The two families require different serving plumbing — don't assume a code path written for one works on the other.

## Example architectures

### LLaVA 1.5 (fixed tiling, token-splice)

- CLIP-ViT-L/14 at 336² → 576 image tokens
- 2-layer MLP projector
- Decoder: Vicuna / Llama 2 family
- Simple: 1 image = 576 tokens; no variable count

### LLaVA-NeXT (dynamic high-res tiling)

- Same encoder (CLIP-ViT-L/14 336²)
- **Dynamic grid**: pick best-fit grid from `{1×1, 1×2, 2×1, 1×3, 3×1, 2×2, 1×4, 4×1}` minimizing padding; tile image into that grid
- Each tile encoded independently; also include one downsampled thumbnail
- Final token count: `(num_tiles + 1) × 576` — e.g., 2×2 + thumbnail = 2880 tokens per image
- Variable per image; engines must handle variable per-request image-token lengths

### Qwen2-VL / Qwen2.5-VL / Qwen3-VL (NaViT-style)

- Native variable-resolution ViT — processes images near native resolution, bounded by `min_pixels` / `max_pixels` config
- Patch size 14 or 16; **2×2 spatial merge** after the encoder halves each dim
- **M-RoPE**: 3 RoPE axes per token `(t, h, w)` — time, height, width; split each head's RoPE dim into three groups
- Text tokens: `t = h = w = position`; image tokens: shared `t`, varying `h, w`; video tokens: `t` = frame index
- Video: sample N frames + 2-frame temporal merge; concatenate into LLM stream
- Qwen3-VL-MoE and Qwen3-Omni-MoE extend to MoE-decoder variants

### mllama (Llama-3.2 Vision, cross-attention)

- Separate vision encoder (Meta's custom ViT-H)
- Decoder: Llama-3.1 text + **cross-attention layers inserted periodically**
- Image tokens **don't splice** into the text sequence — they feed cross-attention K/V
- Different serving path: two KV caches (self and cross) per cross-attention layer
- Different masking (image-aware attention mask)

### InternVL 2 / 3

- SigLIP / InternViT encoder, dynamic tiling similar to LLaVA-NeXT but with its own grid set
- Token-splice architecture
- Multi-image support

### DeepSeek-VL / DeepSeek-VL2

- Hybrid vision encoder (SigLIP + other)
- Token-splice
- DeepSeek-VL2 uses MoE decoder → intersection with `models/text-moe/`

### Molmo

- CLIP-based, splice architecture
- Adds pointing / grounding capability via special tokens

## Variable-resolution ViT plumbing (NaViT-style)

Unlike fixed-input CLIP, variable-resolution encoders pack multiple images of different sizes into one batch with per-image attention masks:

```
image_1: (H1, W1) → (H1/p, W1/p) patches
image_2: (H2, W2) → (H2/p, W2/p) patches
...
packed_tokens: concat[image_1_patches, image_2_patches, ...]
attn_mask: block-diagonal preventing cross-image attention
```

This affects both the preprocessor and the ViT forward pass. See [`tooling/io-handling/`](../tooling/io-handling.md) for preprocessing details.

## M-RoPE — 3D position encoding

For Qwen-VL-family. Each token carries `(t, h, w)` position IDs. Implementation:

- Head RoPE dim is split into three groups (commonly `head_dim/4, head_dim/4, head_dim/2`)
- Each group applies RoPE with its axis's position ID
- Concatenate the three rotated sub-tensors

Implications:
- Text-only inference on an M-RoPE model *accidentally* works because all three axes carry the same value; add an image and everything breaks silently without M-RoPE support.
- Speculative decoding drafters must advance all three axes.

## Token splicing logistics

1. Tokenize the text prompt with the tokenizer's `<image>` placeholder.
2. Run image preprocessor → image-token embeddings (post-projector).
3. Expand each single `<image>` token into N image-token positions (N depends on image size and model).
4. Replace the placeholder embeddings with the image-token embeddings before the LLM.
5. Position IDs increment through text + image tokens as one sequence (or 3D for M-RoPE).

Mistakes here are the #1 source of vision-language serving bugs.

## Preprocessing

Image / video preprocessing (resize, tile, normalize, patchify, frame sampling, NaViT packing) is covered in [`tooling/io-handling/`](../tooling/io-handling.md). Use HF `AutoProcessor` / per-model processors unless you have reason to reimplement.

## Pitfalls

- **Architecture-family confusion.** A code path for LLaVA (splice) won't work for mllama (cross-attention) without rewriting the decoder integration.
- **Placeholder-token count mismatch.** Tokenizer emits one `<image>` token; the LLM needs hundreds of image-token positions inserted. Get the expansion wrong → position IDs misaligned → silent garbage output.
- **1D RoPE on M-RoPE models.** Works on text-only prompts accidentally (all axes equal); fails on images.
- **Image token count assumptions.** Don't hardcode 576 or 2880 across models; compute from actual image dims and config.
- **`image_min_pixels` / `max_pixels` ignored.** Qwen-VL images silently scale to model-specific bounds; fight this and you get wrong token counts.
- **Video token budget.** Long videos explode token counts — enforce max frames + max tokens upstream.
- **Vision encoder on wrong device.** Moving image embeddings to the LLM GPU per-request dominates preprocessing latency if encoder lives elsewhere.
- **Chat template mismatch.** VL chat templates differ from the base LLM's — use the VL variant.
- **Prefix caching across different images.** Two requests with same text but different images must not share the image-token KV portion — cache key must include image content.
- **Quantization of the vision encoder.** Less tested than decoder quant; accuracy drops are easier to trigger.
- **Cross-attention KV for mllama re-computed every step.** Compute once per request, reuse across decode — silent perf disaster otherwise.

## See also

- [`algorithms/attention-variants/`](../algorithms/attention-variants.md) — cross-attention (mllama) and M-RoPE 3D-position attention in context
- [`tooling/io-handling/`](../tooling/io-handling.md) — preprocessing + placeholder expansion
- [`algorithms/disaggregated-serving/`](../algorithms/disaggregated-serving.md) — vision-worker disaggregation
- [`models/speech-language/`](speech-language.md) — contrast with audio encoder-decoder
- [`models/text-moe/`](text-moe.md) — for MoE-decoder VL variants (Qwen3-VL-MoE, DeepSeek-VL2)
- [`models/omni-multimodal/`](omni-multimodal.md) — when the thinker side is the *first stage* of a unified text + speech + image output model (Qwen-Omni, BAGEL)
