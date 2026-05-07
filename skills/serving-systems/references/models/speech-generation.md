# Speech generation (TTS and STS)

Speech serving differs from text serving in several structural ways — outputs are long codec-token streams decoded by a second model, streaming is the default pattern (TTFA, not just TTFT, is the bar), and voice / prosody / emotion conditioning has no text-LLM equivalent. The model landscape splits into three families plus a shared neural-codec backbone.

## Three architectural families

### (A) Autoregressive codec-token (GPT over codec)

```
text (+ reference audio for cloning) ──► text encoder / phonemizer / chat template
                                                   │
                                                   ▼
                                          AR transformer decoder (causal)
                                                   │
                                                   ▼
                                           codec tokens (one per frame)
                                                   │
                                                   ▼
                                       neural codec decoder (EnCodec / SNAC / DAC / ...)
                                                   │
                                                   ▼
                                                waveform
```

Examples: Bark, Parler-TTS, ChatTTS, Fish-Speech, **Orpheus**, **Qwen3-TTS** (talker branch). Good for voice cloning from a reference clip, expressive control, and long-form. Long output sequences, cumulative acoustic errors.

### (B) Non-autoregressive (flow-matching / diffusion on mel)

```
text ──► phonemizer / G2P ──► duration predictor ─► expanded phoneme frames
                                                         │
                                                         ▼
                                          flow-matching / diffusion decoder
                                                         │
                                                         ▼
                                                   mel spectrogram
                                                         │
                                                         ▼
                                              HiFi-GAN-style vocoder ─► waveform
```

Examples: StyleTTS 2, **Kokoro**, F5-TTS, Matcha-TTS. Low TTFA, predictable latency. Less expressive than AR; in-context voice cloning is newer.

### (C) Hierarchical / depth-refinement hybrid (dominant in 2025)

```
text + speaker cond ──► backbone AR transformer ─► first codebook token c0
                                                         │
                                                         ▼
                                      depth transformer refines c1..c_{N-1}
                                                         │
                                                         ▼
                                             full RVQ codec frame
                                                         │
                                                         ▼
                                                 codec decoder
```

Two transformers: a large backbone produces the first codebook token (semantic), a smaller depth transformer iterates over residual codebooks for that frame. Each "step" of the serving loop runs both. **CSM**, **Qwen3-TTS**, **Chatterbox** follow variants of this.

### STS (speech-in → speech-out)

Same three families, but the input side also takes audio. Usually: audio encoder (Whisper-style) → projector → text LLM with interleaved text + audio tokens → codec decoder. **GLM-4-Voice**, **Step-Audio-2-Mini**, **Moshi** are examples.

## Neural codecs — the shared backbone

Most modern TTS operates on discrete tokens from a neural audio codec. The codec sets the sample rate, the frame rate (tokens/sec), and the codebook structure.

| Codec | Codebooks | Sample rate | Token rate | Used by |
|:------|:----------|:-----------|:-----------|:--------|
| **EnCodec** (Meta) | 8 RVQ | 24 kHz | 75 Hz | Bark, MusicGen |
| **SoundStream** (Google) | 12 RVQ | 16 kHz | 50 Hz | AudioLM |
| **DAC** (Descript) | 9 RVQ | 44.1 kHz | 86 Hz | Parler-TTS, **Zonos** |
| **SNAC** (Hubert) | 7 hierarchical | 24 kHz | variable | **Orpheus** |
| **Mimi** (Kyutai) | 32 RVQ (first for semantic, rest acoustic) | 24 kHz | 12.5 Hz | **CSM**, Moshi |
| **S3 / S3Gen** | 1 stream (6561 tokens) | 24 kHz | ~25 Hz | **Chatterbox**, **CosyVoice2** |
| **Qwen3-TTS codec** | 16 @ ~8192 cardinality | 24 kHz | 12.5 Hz | **Qwen3-TTS** |
| **X-Codec-2** | RVQ | 24 kHz | — | newer AR TTS |

Key property: **token rate × seconds = AR-sequence length.** A 10s output at 75 Hz = 750 tokens; at 12.5 Hz (Mimi / Qwen3-TTS / CSM) = 125 tokens. Lower token rate ⇒ huge latency win on AR, but requires the codec to carry more information per token.

## VoxServe model catalog

[VoxServe](https://github.com/vox-serve/vox-serve) is a streaming-centric serving system built specifically for speech-LMs. It unifies the three families plus STS models behind one serving loop. Reading its `vox_serve/model/*.py` is the cleanest way to see how diverse these architectures really are — each file adapts a distinct model to a common `BaseLM` (or `BaseLMWithDepth`) interface.

### Chatterbox (ResembleAI) — TTS

- **Family**: hybrid cascade (T3 text-to-acoustic + S3Gen acoustic-to-waveform)
- **Backbone**: ~520M Llama-style T3 transformer (1024 hidden, 30 layers, 16 heads)
- **Codec**: custom single-stream (6561 speech tokens + 1 stop) → S3Gen decoder @ 24 kHz
- **Conditioning**: speaker embedding (256-dim voice encoder) + reference speech-prompt tokens + **emotion scalar** (0.0–1.0) fused via perceiver resampler
- **Distinctive**: dual-stream conditioning (identity + style + emotion) in one forward
- **Output**: 24 kHz mono

### CosyVoice2-0.5B (FunAudioLLM) — TTS

- **Family**: NAR flow-matching (LLM for semantic tokens + flow decoder for acoustic)
- **Backbone**: CosyVoice2-0.5B (896 hidden, 24 layers, 14 heads, **GQA 7:1** with 2 KV heads)
- **Codec**: S3-v2 speech tokenizer (6561 tokens); flow encoder/decoder + HiFT vocoder
- **Conditioning**: CAMPlus speaker embedding + reference mel features + reference speech tokens
- **Distinctive**: flow-based parallel acoustic decoding; supports **prompt-cache mode** for sharing decoder state across requests with the same reference voice
- **Output**: 24 kHz mono

### CSM-1B (Sesame) — TTS

- **Family**: hierarchical depth-refinement over Mimi codec
- **Backbone**: 768-hidden, 21-layer transformer → depth decoder (1536 hidden, 9 layers, covers codebooks 1–31)
- **Codec**: **Mimi @ 12.5 Hz**, 32 codebooks (cardinality 2048) — very low token rate
- **Conditioning**: conversational speaker prompts (up to 2 reference speakers' audio + text interleaved into context)
- **Distinctive**: per-frame depth-first codec generation — backbone emits `c0`, depth transformer autoregresses `c1..c31` within the same frame before advancing
- **Output**: 24 kHz mono; frame cadence 12.5 Hz → one "step" produces 80 ms of audio

### Orpheus-3B (Canopy Labs) — TTS

- **Family**: single-stage AR codec-token
- **Backbone**: Orpheus-3B (2048 hidden, ~40 layers, 16 heads)
- **Codec**: SNAC @ 24 kHz, 7-codebook structure packed into 28-token sequences via index arithmetic `(token - 128256 - 10) % 4096`
- **Conditioning**: **voice name token prefix** (e.g., `[tara]`) — 8 built-in voices, no explicit speaker embedding
- **Distinctive**: extreme simplicity — single AR stream, clever token packing, no side conditioning
- **Output**: 24 kHz mono

### Qwen3-TTS-1.7B (Alibaba) — TTS

- **Family**: hierarchical cascade (talker + depth code predictor)
- **Backbone**: Qwen3-TTS-1.7B (2048 hidden, 16 heads, 16 layers) + depth code predictor (1024 hidden, 5 layers, covers 16 code groups)
- **Codec**: custom 12.5 Hz tokenizer, 16 codebooks (~8192 cardinality each)
- **Conditioning**: **in-context learning** — reference audio codec tokens prepended as embeddings; depth predictor accumulates embeddings across iterations for strong voice consistency
- **Distinctive**: ICL voice cloning; VoxServe hits **40 ms TTFA on H100** with the 12Hz variant
- **Output**: 24 kHz mono

### Zonos-v0.1 (Zyphra) — TTS

- **Family**: NAR delay-pattern codec (all codebooks generated in parallel, position-dependent heads)
- **Backbone**: **SSM + attention hybrid** (1024 hidden, 16 layers, 8 heads, **GQA 2:1** with 4 KV heads)
- **Codec**: DAC @ 44.1 kHz, 9 codebooks
- **Conditioning**: LDA speaker embedding + **emotion vector (8 dims)** + Fourier-projected prosody controls (pitch std, speaking rate) + language ID + VQ / DNSMOS quality targets
- **Distinctive**: richest control surface (emotion + prosody + quality); delay-pattern generation enables parallel multi-codebook output; hybrid Mamba+attention backbone
- **Output**: 44.1 kHz native, resampled to 24 kHz for serving

### GLM-4-Voice-9B (Zhipu AI / THU) — STS

- **Family**: hybrid cascade STS (GLM backbone with interleaved text+audio tokens → flow decoder + HiFT vocoder)
- **Backbone**: GLM-4-Voice-9B (4096 hidden, 40 layers, 32 heads, **MQA-style** with 2 KV heads)
- **Codec**: audio tokenizer → flow-based decoder + HiFT vocoder @ 44.032 kHz
- **Conditioning**: audio input via interleaved text/audio tokens (system prompt enforces **13 text tokens + 26 audio tokens** repeating pattern)
- **Distinctive**: end-to-end speech-in → speech-out; speech-aware LLM answers in audio with natural pauses
- **Output**: 44.032 kHz mono

### Step-Audio-2-Mini (StepFun) — STS

- **Family**: hybrid cascade STS (audio encoder + LLM + Conformer vocoder)
- **Backbone**: 3584 hidden, 28 layers, 28 heads, **GQA 7:1** (4 KV heads) + audio encoder (1280 hidden, 32 layers, 20 heads) + adaptor
- **Codec / input**: 128-mel @ 16 kHz input (STFT 400, hop 160); Conformer estimator + HiFT vocoder for 24 kHz output
- **Conditioning**: mel-spectrogram audio input; speaker identity from reference audio in prefix (if provided)
- **Distinctive**: explicit mel-in path (Whisper-style encoder + adaptor bottleneck), parallel Conformer-based vocoder
- **Output**: 24 kHz mono

## The VoxServe serving abstraction (common pattern)

Reading `vox_serve/model/base.py` + the per-model files reveals a consistent adapter pattern:

```
preprocess()  — text / audio → token frames + input features + masks
forward()     — streaming generation via KV cache (+ depth cache for hierarchical)
sampling()    — logits → codec tokens (with repetition penalty, etc.)
postprocess() — codec tokens → audio via model-specific decoder
```

Each model exposes `n_codebooks`, `vocab_size`, `detokenize_interval` (how often to call the codec decoder, in steps), and `detokenize_overlap` (how many frames to share between decoder calls). Hierarchical models subclass `BaseLMWithDepth` to add a depth-transformer pass per frame.

Streaming uses **FlashInfer-managed KV caches** for the backbone plus model-specific decoder caches (CosyVoice2 flow, Step-Audio Conformer). The scheduler batches across requests by normalizing `output_audio_length` (in samples) to a common tick cadence. The result: 8 architecturally very different models coexist in one serving loop.

Key design lessons a serving engineer can take:

- **Per-model `detokenize_interval` tuning** trades TTFA against codec-decoder call overhead. Orpheus uses 28, CSM 10, Zonos 50 — picked empirically for each codec / model pair.
- **Prompt-cache sharing** (CosyVoice2) is the TTS analog of prefix caching: same reference voice → shared decoder state across requests.
- **Depth-transformer batching** is a new scheduling problem: you have two AR models with coupled state, and their per-frame ratio is fixed.
- **Sample-rate normalization** happens at the scheduler level (44.1 → 24 kHz for Zonos / GLM) so the output stream cadence is uniform.

## Cross-family patterns

### Streaming audio output

The dominant serving pattern for speech:

- **AR / hierarchical**: every K frames, run the codec decoder on the buffer → emit the corresponding audio chunk. K is `detokenize_interval`.
- **NAR flow / diffusion**: generate the whole mel / latent, vocoder decodes in chunks — lower TTFA if the flow step count is small.

**TTFA (time to first audio)** is the user-facing metric. Typical production target: ≤ 200 ms.

### Voice cloning mechanisms

| Mechanism | Models using it |
|:----------|:----------------|
| Speaker embedding (pre-computed from reference) | Chatterbox, CosyVoice2, Zonos (LDA), XTTS v2 |
| **In-context learning** (reference tokens prepended) | **Qwen3-TTS**, CSM, F5-TTS |
| Acoustic prompt (reference codec tokens as prompt) | Bark |
| Prompt prefix / voice name | Orpheus (fixed set), Parler-TTS (style prompt) |
| None (single voice or random per seed) | Kokoro |

Pre-compute speaker embeddings per voice and cache by speaker ID — encoding reference audio on the hot path is a ~100ms+ per-request cost.

### Conditioning inputs beyond voice

Zonos and Chatterbox expose emotion / prosody control vectors. Qwen3-TTS and CosyVoice2 expose style via system prompts or text descriptors. Step-Audio and GLM take full audio input (STS). When designing the API:

- Voice ID / embedding → part of request
- Emotion / prosody → separate vectors, often floats
- Style description → text field (some models)
- Reference audio → separate file upload

### Batching

AR TTS batches like text LLMs, with longer / variable output lengths. NAR TTS batches trivially. Hierarchical models (CSM, Qwen3-TTS) batch across the backbone AND the depth transformer — two coupled schedulers if you want to saturate both.

## Serving stack

TTS / STS serving sits outside the mainstream LLM-serving engine ecosystem:

- **Model-specific**: Bark.cpp, StyleTTS2, mlx-lm audio (for Macs)
- **Speech-LM-specific**: [VoxServe](https://github.com/vox-serve/vox-serve) — unifies the 8 models above; built around streaming, per-model codec scheduling
- **Frameworks**: HuggingFace Transformers + FastAPI for simple setups; TorchServe / Triton Inference Server for wrapping
- **Notable integrations**: SGLang has paths for Moshi / Mimi-based models; other engines typically do not
- **Audio diffusion (SoundStorm-like)**: separate tool chain

Expect to write or extend the serving layer for anything beyond these paths.

## Pitfalls

- **Codec mismatch.** Model trained on DAC 44.1 kHz doesn't work with an EnCodec decoder. Codec + model are tightly coupled; substituting silently produces noise.
- **Token-rate confusion.** 12.5 Hz vs 50 Hz vs 75 Hz vs 86 Hz codec → different "seconds per N tokens" math. Off-by-one here produces subtly-wrong durations.
- **Hierarchical depth-transformer starvation.** If the backbone runs at batch B but depth runs at batch 1 per frame, the depth pass serializes requests — CSM / Qwen3-TTS need explicit depth batching.
- **Vocoder quality loss under quantization.** AR backbone quantizes fine to FP8; vocoders are sensitive — artifacts appear as audible hiss or warble.
- **Streaming cadence too coarse.** Emitting per-utterance destroys the realtime feel. Aim for ≤ 200 ms chunks; models like Qwen3-TTS reach 40 ms TTFA on H100.
- **Reference encoding on the hot path.** Pre-encode speaker / voice embeddings; cache by speaker ID.
- **TTFT vs TTFA.** "Time to full utterance" is the wrong metric for TTS — users care about first audio chunk.
- **Long-form coherence.** Chunked AR TTS loses prosody coherence across chunks; add overlap + crossfade.
- **G2P / phonemizer drift.** Different phonemizers for the same model → different outputs. Pin the phonemizer.
- **Treating TTS as text LLM.** Most LLM-engine techniques don't transfer cleanly — expect to own more of the serving stack, or use VoxServe.
- **Sample-rate normalization forgotten.** Mixed-rate outputs (Zonos 44.1 kHz, Qwen3-TTS 24 kHz) need uniform downstream cadence.
- **STS system-prompt pattern mismatch.** GLM-4-Voice expects precisely interleaved 13 text + 26 audio tokens; breaking this produces garbage output.
- **Mel-input sample-rate drift.** Step-Audio expects 16 kHz STFT; upstream resampling errors silently kill encoder output quality.

## See also

- [`models/speech-language/`](speech-language.md) — audio-input (ASR) counterpart
- [`models/omni-multimodal/`](omni-multimodal.md) — when the TTS stack is the "talker + code2wav" end of a full omni model (Qwen-Omni, MiMo-Audio)
- [`algorithms/continuous-batching/`](../algorithms/continuous-batching.md) — AR TTS fits this pattern; hierarchical models add a second batching axis
- [`algorithms/attention-variants/`](../algorithms/attention-variants.md) — Zonos is SSM+attention hybrid; many backbones are standard GQA/MQA
- [`tooling/io-handling/`](../tooling/io-handling.md) — streaming audio output, SSE / chunked HTTP
- [`OVERVIEW.md`](../../OVERVIEW.md) — generative-media scope caveat
- VoxServe source: <https://github.com/vox-serve/vox-serve> — concrete implementations of all 8 models above
