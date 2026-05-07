# Omni-multimodal models

Models that accept multiple input modalities and **emit more than one output modality**. They don't fit the vision-language (multi-in, text-out) or speech-generation (text-in, audio-out) buckets because they cross both. Two architectural families dominate.

## Two architectural families

### (A) Thinker-Talker-Code2Wav (omni-in → text + streaming speech-out)

```
text + image + video + audio ──► THINKER (omni understanding LLM)
                                      │
                                      ├──► text tokens (user-visible)
                                      │
                                      └──► hidden states + text embeddings
                                                   │
                                                   ▼
                                           TALKER (codec-token AR)
                                                   │
                                                   ▼
                                      RVQ codec codes (multi-layer)
                                                   │
                                                   ▼
                                        CODE2WAV / TOKEN2WAV decoder
                                                   │
                                                   ▼
                                             streaming waveform
```

Three separate transformers (plus the codec decoder) wired in a pipeline. Thinker does the heavy multimodal lift and emits user-visible text + hidden states. Talker converts those into first-codebook codec tokens. Code predictor (typically an MTP-style head) fills codebooks 1..N-1. Code2Wav upsamples the RVQ stack into an audio waveform, streamingly.

Used by: **Qwen3-Omni**, **Qwen2.5-Omni**, **MiMo-Audio**, **Ming-flash-omni 2.0**, **Dynin-Omni**.

### (B) Unified image understanding + generation

```
interleaved text + image tokens ──► shared backbone (per-modality experts)
                                          │
                                          ├──► text tokens (AR)
                                          │
                                          └──► image latents (flow-matching / diffusion)
                                                    │
                                                    ▼
                                                VAE decoder ─► pixel image
```

One backbone, two heads. Text emerges AR; images emerge via a flow-matching / diffusion step over VAE latents. The backbone routes per-modality at the **FFN level** — a Mixture-of-Transformers (MoT) — so self-attention is shared across modalities but MLPs are per-modality. Dual visual encoders: one semantic ViT (for understanding), one VAE encoder (for generation).

Used by: **BAGEL**, **Hunyuan-Image3**, **MammothModa2**, **GLM-Image AR**.

## Example architectures

### Qwen3-Omni-30B-A3B (Alibaba, MoE thinker + MoE talker + code2wav)

- **Modalities**: text / image / video / audio in; text + streaming speech out. 119 text languages, 19 speech-in, 10 speech-out.
- **Variants**: `Instruct` (full thinker + talker), `Thinking` (thinker only, adds CoT, text-out), `Captioner` (thinker only, fine-tuned for audio captioning).
- **Naming**: `30B-A3B` = 30B total MoE parameters, ~3B activated per token. **Both thinker and talker are MoE.**
- **Thinker**: Qwen3-MoE LLM (class `Qwen3MoeLLMForCausalLM`) + `Qwen3OmniMoeAudioEncoder` (AuT — Audio Transformer, Whisper-lineage) + `Qwen3Omni_VisionTransformer` (Qwen2.5-VL-style ViT with M-RoPE).
- **Talker**: MoE transformer; consumes thinker text embeddings + hidden states via `text_projection` + `hidden_projection`; emits codec layer 0 via `codec_head`.
- **Code predictor**: `Qwen3OmniMoeTalkerCodePredictor` — an **MTP (multi-token prediction) head** fills codebooks 1..N-1 in parallel.
- **Code2Wav**: `Qwen3OmniMoeCode2Wav` — code embedding → sliding-window-attention pre-transformer → ConvNeXt upsample → multi-stage causal trans-conv (`SnakeBeta` activation) → 16 kHz waveform. ~1280× upsample factor.
- **Streaming**: first audio packets emit before full text finishes; `realtime_max_tokens = 64`.
- **Voice presets**: Ethan / Chelsie / Aiden.

### Qwen2.5-Omni (predecessor, dense + DiT vocoder)

- Same thinker-talker split, dense (non-MoE).
- Audio output path is **Token2Wav** (not Code2Wav): `Qwen2_5OmniToken2WavForConditionalGeneration` + `Qwen2_5OmniToken2WavDiTModel` — a DiT vocoder rather than RVQ + ConvNeXt.
- Qwen2.5-Omni-Thinker ships separately as a vision-audio-LM (understanding-only). That's what `models/vision-language/` and `models/speech-language/` primarily refer to; the full omni path with talker + Token2Wav lives here.

### MiMo-Audio (Xiaomi)

- `MiMoAudioForConditionalGeneration` (unified) + `MiMoAudioLLMForConditionalGeneration` (thinker) + `MiMoAudioToken2WavForConditionalGenerationVLLM` (token2wav).
- Audio in / text + audio out; same thinker-talker-vocoder shape as Qwen-Omni.

### Ming-flash-omni 2.0 (Ant / BailingMM2)

- `MingFlashOmniForConditionalGeneration` + `MingFlashOmniThinkerForConditionalGeneration`.
- HF `config.json` architecture name: `BailingMM2NativeForConditionalGeneration`.
- Thinker-first pattern; serving-time thinker can be deployed alone.

### Dynin-Omni

- `DyninOmniForConditionalGeneration` — another thinker-talker omni model, newer entrant.

### BAGEL-7B-MoT (ByteDance Seed)

- **Paper**: "Emerging Properties in Unified Multimodal Pretraining" (arXiv 2505.14683).
- **Modalities**: text + image in, text + image out (no audio). Free-form image editing, multi-view synthesis, early world-modeling.
- **Architecture**: **Mixture-of-Transformers (MoT)** on Qwen2 decoder layers — two modality-expert FFN stacks share a single self-attention per block. 7B active / **14B total** parameters.
- **Dual visual encoders**:
  - **ViT** for semantic features (drives understanding)
  - **VAE encoder** for pixel-level latents (drives generation / editing)
- **Token fusion**: interleaved text + ViT patches + VAE latent patches, all co-attended in shared self-attention; modality-specific experts route feed-forward.
- **Training objective**: "Next Group of Token Prediction" — predict next group of language or visual tokens.
- **Output heads**:
  - Text: standard AR next-token
  - Image: **flow-matching / rectified-flow** over VAE latents, with CFG knobs (`cfg_text_scale`, `cfg_image_scale`, `cfg_interval`, `timestep_shift`, `num_timesteps`, `cfg_renorm_type`)
- **Benchmarks**: MMMU 55.3, MathVista 73.1, MME 2388 on the understanding side; GenEval 0.82 (0.88 with CoT rewriter) on image gen, beating SD3-Medium; GEdit-Bench-EN 7.36 SC / 6.83 PQ on editing.

### Hunyuan-Image3 (Tencent)

- `HunyuanImage3ForCausalMM` — AR-based image-output model, registered in vllm-omni.
- Shows up in both the AR registry and the separate diffusion stack.

### MammothModa2

- AR + DiT pipeline — `MammothModa2ARForConditionalGeneration` + `MammothModa2DiTPipeline`. AR portion produces plan tokens / text, DiT pipeline handles image denoising.

### GLM-Image AR

- `GlmImageForConditionalGeneration` — image output via AR on visual tokens, not diffusion.

## How the stages connect — serving implications

### Staged pipeline (thinker / talker / code2wav) enables disaggregation

Each stage is a separately registered module with its own `vllm_config`. vllm-omni exposes `model_stage` (`"thinker"` / `"talker"` / `"code2wav"`) so the three can run on different GPUs or nodes with an `OmniConnector` streaming between them. This matches the disaggregated-serving pattern at [`algorithms/disaggregated-serving/`](../algorithms/disaggregated-serving.md) but with three stages instead of two (prefill / decode → thinker / talker / code2wav).

### Multi-codebook streaming output

The RVQ codec model forces the talker to emit multiple codebooks per frame. Strategies:

- **Depth-AR**: layer-0 token AR → small transformer iterates layers 1..N-1 (CSM-style, Qwen3-TTS-style).
- **MTP / parallel head**: a multi-token-prediction head outputs layers 1..N-1 in parallel (Qwen3-Omni's `Qwen3OmniMoeTalkerCodePredictor`). Lower latency; relies on independence assumptions across codebooks.

### Dual-encoder handoff

BAGEL-family unified models carry **two image representations**: the semantic ViT path (understanding) and the VAE latent path (generation). Both are written into the same sequence and read by the shared attention. Engines must:

- Allocate two encoder forward passes per image input at prefill (or cache one if only understanding is needed).
- Track the VAE-latent vs ViT-patch boundary in the sequence for downstream diffusion steps.

### Image output = denoising loop inside the LLM step

For unified image-gen models (BAGEL, MammothModa2 DiT stage), image emission is not a single forward — it's **N denoising steps** over VAE latents. The surrounding engine needs to dispatch to the diffusion loop the way it dispatches to an AR decoder. vllm-omni splits this via a parallel `vllm_omni/diffusion/` stack (engine, scheduler, worker, model loader) alongside the AR path.

### MoE pressure in talker + code predictor

Qwen3-Omni's talker is its own MoE (not just the thinker). Serving the full pipeline requires two MoE memory budgets plus the code-predictor MTP head — an argument for running talker + code2wav on a different tier than thinker.

## Compatibility with the rest of the collection

| Concern | Notes |
|:--------|:------|
| Continuous batching | ✓ at each stage (thinker, talker, code2wav are each AR or diffusion-step AR) |
| Paged attention | ✓ in thinker + talker (standard) |
| Speculative decoding | ✓ on thinker text output; not standard on talker codec tokens |
| MTP | used natively in Qwen3-Omni's code predictor |
| Quantization | FP8 viable on thinker + talker; code2wav is dense convolutional, usually FP16/BF16 |
| CUDA graph capture | thinker = LLM pattern; talker = smaller LLM pattern; code2wav = fixed-shape convolutional (ideal capture target) |

## Engine / source pointers

Primary serving implementation for these models: [`vllm-omni`](https://github.com/vllm-project/vllm-omni), a vLLM extension for omni + diffusion workloads.

| Model | vllm-omni location |
|:------|:-------------------|
| Qwen3-Omni | `vllm_omni/model_executor/models/qwen3_omni/{qwen3_omni,qwen3_omni_moe_thinker,qwen3_omni_moe_talker,qwen3_omni_moe_code_predictor_mtp,qwen3_omni_code2wav}.py` |
| Qwen2.5-Omni | `vllm_omni/model_executor/models/qwen2_5_omni/{qwen2_5_omni,..._thinker,..._talker,..._token2wav}.py` |
| BAGEL | `vllm_omni/model_executor/models/bagel/bagel.py` (wraps `vllm.model_executor.models.bagel.BagelForConditionalGeneration`) + `vllm_omni/diffusion/models/bagel/{autoencoder,bagel_transformer,pipeline_bagel}.py` |
| MiMo-Audio | `vllm_omni/model_executor/models/mimo_audio/` |
| Ming-flash-omni | `vllm_omni/model_executor/models/ming_flash_omni/` |
| Registry | `vllm_omni/model_executor/models/registry.py` (dict `_OMNI_MODELS`) |
| Common serving interface | `vllm_omni/model_executor/models/output_templates.py` (`OmniOutput`) + `vllm_omni/model_executor/custom_process_mixin.py` |

The Qwen-Omni **thinker-only** variants also live partially in upstream vLLM (`vllm/model_executor/models/qwen2_5_omni_thinker.py`, `qwen3_omni_moe_thinker.py`) — those are the vision-language-style understanding cores. The full omni pipeline (thinker + talker + code2wav) requires vllm-omni.

## Pitfalls

- **Thinker-only ≠ omni.** Serving just the thinker (e.g. via stock vLLM) gives you vision-language + audio understanding → text. Full omni with speech output requires the talker + code2wav stages too.
- **MoE thinker + MoE talker.** Two MoE budgets; don't assume one EP plan fits both stages. Talker has its own expert count and router.
- **RVQ codebook independence.** MTP-style parallel code prediction assumes cross-codebook independence — acceptable quality in practice but not guaranteed. Depth-AR is safer but higher latency.
- **BAGEL's dual encoders.** Missing the VAE encoder path silently disables image-editing / generation while understanding still works. The image-input token expansion has to match whichever encoder is active.
- **Flow-matching CFG doubles batch** just like standard diffusion — BAGEL generation is not free in batch terms.
- **Voice presets are baked in.** Qwen3-Omni ships with Ethan / Chelsie / Aiden; voice cloning at serve time is not part of the base model.
- **Streaming latency accounting.** Omni serving has two TTFT-analogues: text-TTFT (thinker emits first token) and audio-TTFA (code2wav emits first waveform sample). These are different and must be reported separately.
- **Understanding-only baselines.** Comparing an omni model to a vision-language baseline on a text-out benchmark is valid, but MMLU-style scores don't capture the speech-out capability the model is actually trained for.

## See also

- [`models/vision-language/`](vision-language.md) — thinker-only visual understanding component
- [`models/speech-language/`](speech-language.md) — speech-in counterpart (Whisper-lineage audio encoders)
- [`models/speech-generation/`](speech-generation.md) — codec + talker + vocoder patterns that omni speech-out reuses
- [`models/image-generation/`](image-generation.md) — flow-matching and DiT patterns BAGEL's image-out path borrows
- [`models/text-moe/`](text-moe.md) — MoE thinker + talker architectures
- [`algorithms/disaggregated-serving/`](../algorithms/disaggregated-serving.md) — the 3-stage (thinker / talker / code2wav) split is disaggregation-friendly
- [vllm-omni](https://github.com/vllm-project/vllm-omni) — the primary open-source serving implementation
