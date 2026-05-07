# Speech-language models (audio in → text out)

Audio-understanding serving: speech recognition, translation, audio question answering. Architectures split into two families based on how audio meets the language model.

## Two architectural families

### (A) Encoder-decoder (classic ASR)

```
audio waveform ─► log-mel ─► audio encoder (one-shot forward)
                                    │
                                    ▼
                            encoder hidden states ──► cross-attention K,V
                                                            │
<start-of-transcript> ... ──► decoder (autoregressive) ─────┘
                                │
                                ▼
                          transcript tokens
```

The encoder runs once per utterance. The decoder autoregresses using **two KV caches per layer**: self-attention KV over prior decoded tokens (grows per step), and cross-attention KV derived from encoder outputs (constant after encoder finishes).

**Used by**: Whisper (the canonical example), Whisper-fine-tunes.

### (B) Audio encoder → text LLM (modern "audio-language")

```
audio ─► log-mel / features ─► audio encoder (Whisper-style ViT-over-audio)
                                     │
                                     ▼
                                adapter / projector
                                     │
                                     ▼
text tokens + <audio> placeholder ──► LLM decoder (standard) ─► output
```

The audio encoder produces embeddings that splice into the LLM input — same pattern as vision-language token splicing. **No cross-attention** in this family; the decoder is a standard text LLM.

**Used by**: Qwen2-Audio, Qwen3-ASR, SALMONN-ish hybrids, Qwen2.5-Omni / Qwen3-Omni's audio-input thinker path.

The two families need different serving paths.

## Example architectures

### Whisper (encoder-decoder)

- **Encoder**: 2D conv stem over log-mel → positional embeddings → transformer encoder (layers depend on size: tiny/base/small/medium/large/large-v3)
- **Decoder**: transformer decoder with self-attn + cross-attn per layer
- **Log-mel**: 80 mel bins for ≤v2, **128 for large-v3** (mismatched extractor = silently wrong outputs)
- **30s chunking**: audio padded / trimmed to 30-second windows; longer inputs split into chunks with context carryover via `condition_on_previous_text`
- Special tokens control the decoder: `<|startoftranscript|>`, language (auto or forced), `<|transcribe|>` vs `<|translate|>`, `<|notimestamps|>` or timestamp tokens like `<|0.00|>`

### Qwen2-Audio (audio encoder + text LLM)

- Audio encoder: Whisper-style (encoder only)
- Projector aligns encoder output to LLM hidden dim
- Text decoder: Qwen2 LLM
- Supports audio-QA, captioning, translation via prompting
- Serving: like Qwen2-VL, but audio preprocessing instead of image

### Qwen3-ASR / Qwen3-ASR-Realtime / Qwen3-ASR-Forced-Aligner

- Successor family; adds realtime streaming ASR and forced-alignment variants
- Realtime: chunked encoder forward + streaming decoder
- Forced aligner: produces word-level timestamps

### Qwen2.5-Omni-Thinker / Qwen3-Omni-MoE-Thinker

- Multimodal ("Omni") thinker-side: audio, vision, text input
- Audio path is an audio encoder + projector, same pattern as Qwen2-Audio
- MoE variant intersects with [`models/text-moe/`](text-moe.md)

### SALMONN (dual-encoder)

- Whisper encoder + BEATs audio encoder in parallel
- Both feed a Q-Former-like adapter → LLM
- Richer audio representation (speech + general audio) at serving cost

## Feature extraction (shared)

```python
import librosa
# Load + resample
waveform, sr = librosa.load(path, sr=16000, mono=True)
# (Whisper) pad / trim to 30s
# Log-mel
mel = librosa.feature.melspectrogram(y=waveform, sr=16000, n_fft=400, hop_length=160, n_mels=128)
log_mel = np.log(np.maximum(mel, 1e-10))
```

Or use HF `AutoProcessor` / `WhisperFeatureExtractor`. Feature extraction on CPU is a common bottleneck at high concurrency — move to GPU when possible.

See [`tooling/io-handling/`](../tooling/io-handling.md).

## Serving mechanics

### Encoder-decoder (Whisper family)

- **Encoder runs once per utterance.** For 30s audio, one forward pass of the encoder. Expensive; batch multiple audio inputs when possible.
- **Two KV caches per decoder layer**:

  | Cache | What it holds | Grows? |
  |:------|:--------------|:-------|
  | Self-attn KV | K, V from prior decoded tokens | yes, each step |
  | Cross-attn KV | K, V projected from encoder hidden states | **no** — computed once from encoder output |

- Cross-attn K and V are projected *once* at decoder start (each decoder layer has its own K_enc / V_enc projections of the shared encoder output) and reused every decode step. Recomputing per step is a silent perf disaster.
- **Batched decode across requests**: each request has its own audio → its own cross-attn KV tensor. Batch by padding cross-attn to the max T_enc and masking.

### Audio-encoder + text-LLM

- Standard LLM serving path on the decoder side.
- Audio encoder: run once per request; cache the projected embeddings.
- Splice embeddings into the text sequence at `<audio>` placeholder.

## Streaming ASR

For real-time transcription:

- **Streaming encoder**: chunked encoder forward on rolling audio (every ~300ms); concatenate to prior encoder output.
- **Streaming decoder**: autoregressive output, yielded as generated.
- **Latency is first-chunk latency**: encoder forward + first decoder step. Batching across streams is tricky — each stream has a different "current time".
- Engines with realtime ASR support often ship dedicated paths (`qwen3_asr_realtime`).

## Pitfalls

- **Recomputing cross-attention KV each step.** Silent perf disaster (essentially re-encoding each token).
- **Mel-bin mismatch.** Whisper-v3 uses 128 mel bins, earlier use 80 — mismatched extractor silently yields wrong outputs.
- **Language token omitted.** Whisper needs a language token; missing → wrong-language output with no error.
- **Feature extraction on CPU.** Log-mel on CPU bottlenecks concurrent streams.
- **Audio chunk continuity.** Sequential 30s chunks need `condition_on_previous_text` to preserve context — trades correctness for error accumulation.
- **Timestamps parsing.** Timestamp tokens encode floats at 0.02s granularity; reconstruct segments carefully.
- **Beam search habit.** Whisper quality is historically better with beam search; serving usually uses greedy + rep-penalty. Document which.
- **Forced language on multilingual audio.** Forcing a wrong language produces confident garbage.
- **Placeholder-token count for audio-LLM family.** Similar to vision: one `<audio>` token in text expands to N audio-embedding positions.
- **Realtime vs batch decoding paths.** Don't share code — they have different batching semantics.

## See also

- [`algorithms/attention-variants/`](../algorithms/attention-variants.md) — cross-attention semantics and the "compute once, reuse" rule for cross-attn KV
- [`tooling/io-handling/`](../tooling/io-handling.md) — log-mel extraction
- [`algorithms/continuous-batching/`](../algorithms/continuous-batching.md) — encoder-decoder batching specifics
- [`models/vision-language/`](vision-language.md) — contrast splice vs cross-attention families
- [`models/speech-generation/`](speech-generation.md) — audio *output* counterpart
- [`models/omni-multimodal/`](omni-multimodal.md) — when audio-in is combined with text + speech *out* in one unified model (Qwen-Omni, MiMo-Audio)
