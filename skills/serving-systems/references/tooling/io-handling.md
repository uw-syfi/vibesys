# Input / output handling

Everything that happens *around* the forward pass — request in, response out. The name is generic because multimodal serving turns tokenization into one of several pre/post-processing concerns.

## Input side

### Text tokenization

Rule 1: **apply the chat template exactly once**. When the server receives structured chat messages, use `tokenizer.apply_chat_template`; when `/v1/completions` receives a raw prompt string, treat it as already-owned by the client and do not wrap it again unless the API explicitly says the prompt is unformatted. Hand-formatting chat messages and double-templating client-formatted prompts are both common sources of subtly-wrong outputs.

```python
prompt = tokenizer.apply_chat_template(
    messages=[
        {"role": "system", "content": "..."},
        {"role": "user",   "content": "..."},
    ],
    tokenize=False,
    add_generation_prompt=True,
)
input_ids = tokenizer(prompt, return_tensors="pt").input_ids
```

Gotchas:

- **BOS**: HF tokenizer adds `<|begin_of_text|>` (or similar) automatically in many families. Calling `tokenize=True` with the template already including BOS can double-add it.
- **Preformatted completions prompts**: benchmark clients often call `apply_chat_template(..., tokenize=False, add_generation_prompt=True)` themselves and send the resulting string to `/v1/completions`. Detect model template markers such as `<|begin_of_text|>`, `<|start_header_id|>`, or `[INST]`, or expose a `prompt_is_preformatted` flag. Tokenize these prompts with `add_special_tokens=False`; do not inject another system/user wrapper.
- **Missing pad token**: set `tokenizer.pad_token = tokenizer.eos_token` if needed, but be aware — if the model is actually trained with a separate pad, EOS-as-pad causes attention-mask issues.
- **Chat templates with tool definitions**: some families (Llama-3.1+, Qwen2.5+) expect tools in the system prompt or a dedicated slot.

### Tool-calling prompt formats

Different model families use different tool-call protocols:

| Family | Prompt-side format | Parse-side format |
|:-------|:-------------------|:------------------|
| OpenAI GPT-4-like | JSON in a dedicated field / system prompt | JSON in `tool_calls` field |
| Hermes 2 / 3 | tools in system prompt | `<tool_call>{...}</tool_call>` |
| Llama 3.1+ | tools in system prompt | `<|python_tag|>` + ipython OR JSON |
| Qwen 2.5+ / 3+ | tools in system prompt | `<tool_call>{...}</tool_call>` or OpenAI-style |
| Mistral | tools in system prompt | `[TOOL_CALLS][{...}]` |

Serving engines ship per-protocol parsers. Picking the wrong one produces tool calls that don't parse.

### Image preprocessing

Two architecture families with different preprocessing:

| Model family | Preprocessing |
|:-------------|:--------------|
| **LLaVA / LLaVA-NeXT** | resize to (tiled) 336×336 or 384×384, normalize (CLIP or SigLIP mean/std), patchify |
| **Qwen-VL / Qwen3-VL** | variable resolution (subject to `min_pixels` / `max_pixels` config), patch size 14 or 28, spatial merge 2×2 |

Key operations (inline — avoid PIL slow paths in hot serving):

```python
# Load
img = PIL.Image.open(path).convert("RGB")
# Resize (model-specific)
img = img.resize((H, W), resample=PIL.Image.BICUBIC)
# Normalize
arr = (np.asarray(img, dtype=np.float32) / 255.0 - mean) / std
# Patchify: (H, W, 3) → (H/P, W/P, P*P*3)
patches = arr.reshape(H//P, P, W//P, P, 3).transpose(0, 2, 1, 3, 4).reshape(H//P * W//P, P*P*3)
```

HF `AutoProcessor` / per-model preprocessors do this correctly; use them unless you have a reason.

### Video preprocessing

- **Frame sampling**: usually `target_fps=1..4`, bounded by `max_frames` (e.g., 64). Uniform sampling or keyframe-preferred.
- **Spatial merging**: after frame encode, merge groups of patches (2×2 common).
- **Temporal merging**: merge pairs of frames to reduce token count.

Video token counts blow up quickly — enforce max-tokens upstream.

### Audio preprocessing

Whisper-style:

```python
# Load audio
waveform, sr = librosa.load(path, sr=16000, mono=True)
# Pad or trim to 30s chunks
# Compute log-mel spectrogram (80 or 128 mel bins)
mel = librosa.feature.melspectrogram(y=waveform, sr=16000, n_fft=400, hop_length=160, n_mels=128)
log_mel = np.log(np.maximum(mel, 1e-10))
```

For streaming ASR: chunk audio in 30s windows; overlap + stitch outputs with timestamps.

## Output side

### Streaming detokenization (UTF-8 safety)

Decoding one token at a time can break multi-byte UTF-8 characters across token boundaries. Naive `tokenizer.decode([token_id])` for each new token produces replacement characters or exceptions.

Pattern: **rolling detokenization**.

```python
class IncrementalDetokenizer:
    def __init__(self, tokenizer):
        self.tok = tokenizer
        self.committed_text = ""
        self.committed_ids = []

    def step(self, new_id):
        self.committed_ids.append(new_id)
        full = self.tok.decode(self.committed_ids, skip_special_tokens=False)
        delta = full[len(self.committed_text):]
        # Only yield delta if it ends at a valid UTF-8 boundary:
        if delta and not delta.endswith("�"):  # replacement char indicates partial
            self.committed_text = full
            return delta
        return ""  # hold and wait for next token
```

Real implementations (vLLM, SGLang) have more-polished versions; the shape is the same.

### Tool-call parsing

Parser per family. Input: the model's raw text stream. Output: `(text_chunks, tool_calls)`. Streaming case: partial tool-call JSON arrives chunk-by-chunk; parser must accept fragments and emit complete calls.

Engines ship ready parsers:

- vLLM: `vllm/entrypoints/openai/tool_parsers/`
- SGLang: `python/sglang/srt/function_call/`
- TRT-LLM: handled at the Triton / OpenAI server layer

Use theirs rather than hand-rolling.

### Structured-output extraction

When the model is constrained to a JSON schema (via `algorithms/structured-output`), the output is valid JSON by construction. Post-processing:

- Validate the JSON against the schema (library output should already be valid, but verify).
- Handle incremental streaming: yield partial-object progress or wait for close-brace.

### Stop sequences across streamed chunks

A stop sequence may cross token boundaries. The engine must detect it in the decoded text, not the token stream. Maintain a sliding suffix; check after each token yield. On match, truncate the output at the match start and stop decoding.

### SSE formatting

OpenAI-compatible streaming: each chunk is

```
data: {json-chunk}\n\n
```

End-of-stream:

```
data: [DONE]\n\n
```

See [`tooling/fastapi-serving/#openai-api-spec`](../fastapi-serving/#openai-api-spec) for the exact chunk shape.

## Pitfalls

- **Hand-formatted chat templates.** Always wrong eventually. Use `apply_chat_template`.
- **Tokenizing with / without BOS.** Model trained with BOS but prompt missing it → accuracy drop. Opposite → same problem.
- **Image-placeholder expansion timing.** Tokenizer emits one `<image>` token; the LLM needs N image tokens inserted. Wrong order corrupts positions.
- **Video token explosion.** Unbounded frame count → 100k-token inputs. Enforce limits at preprocessing.
- **Naive decode per token.** Breaks UTF-8; also O(N²) if done by re-decoding the whole prefix each step. Use incremental.
- **Tool-call parser mismatch.** Silent failure — model "calls" a tool but server doesn't parse it, so the response is just text with `<tool_call>` tags leaking through.
- **Stop-sequence match on tokens instead of text.** Tokenizer quirks mean a stop like `\n\n` can be one token or two; match after decoding.
- **Thread-unsafe tokenizers.** Some (older) tokenizers aren't thread-safe; if you're multithreading across requests, use one per thread or per event loop.

## See also

- [`models/vision-language/`](../models/vision-language.md), [`models/speech-language/`](../models/speech-language.md) — per-modality model details
- [`algorithms/structured-output/`](../algorithms/structured-output.md) — the decode-time enforcement side
- [`tooling/fastapi-serving/`](fastapi-serving.md) — HTTP endpoint wiring
