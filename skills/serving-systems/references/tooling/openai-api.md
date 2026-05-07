# OpenAI-compatible API — per-modality contracts

Every OpenAI-compatible serving endpoint shares two shapes:

```
GET  /health              → {"status": "ok"}
GET  /v1/models           → {"object":"list","data":[{"id":"<name>","object":"model","created":1700000000,"owned_by":"local"}]}
```

The real work is per modality. Each section below documents what a serving endpoint must expose to be OpenAI-client-compatible for that modality.

---

## Text generation (causal LM)

### POST /v1/completions

Request (JSON):

| Field | Type | Required | Default | Constraints | Notes |
|:------|:-----|:---------|:--------|:------------|:------|
| `model` | string | optional | server's model | — | identifier (accept any value; clients may pass hints) |
| `prompt` | string or array | **yes** | — | — | text prompt(s) |
| `max_tokens` | integer | optional | 256 | 1–4096 | max tokens to generate |
| `temperature` | float | optional | 1.0 | 0.0–2.0 | |
| `top_p` | float | optional | 1.0 | 0.0–1.0 | nucleus sampling |
| `stop` | string or array | optional | — | — | stop sequence(s) |
| `stream` | boolean | optional | false | — | enable SSE |

Non-streaming response (HTTP 200):

```json
{
  "id": "cmpl-<hex>", "object": "text_completion", "created": 1700000000,
  "model": "<model-name>",
  "choices": [{"text": "...", "index": 0, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 6, "completion_tokens": 20, "total_tokens": 26}
}
```

Streaming (SSE) — each chunk:

```
data: {"id":"cmpl-xxx","object":"text_completion","choices":[{"text":"token","index":0,"finish_reason":null}]}

```

Final chunk carries `finish_reason:"stop"` with empty `text`, followed by `data: [DONE]\n\n`.

### POST /v1/chat/completions

Request:

| Field | Type | Required | Default | Constraints | Notes |
|:------|:-----|:---------|:--------|:------------|:------|
| `model` | string | optional | server's | — | |
| `messages` | array | **yes** | — | — | `[{role, content}]`; roles `"system"`, `"user"`, `"assistant"` |
| `max_tokens` | integer | optional | 256 | 1–4096 | |
| `temperature` | float | optional | 1.0 | 0.0–2.0 | |
| `top_p` | float | optional | 1.0 | 0.0–1.0 | |
| `stop` | string or array | optional | — | — | |
| `stream` | boolean | optional | false | — | |

Non-streaming response:

```json
{
  "id": "chatcmpl-<hex>", "object": "chat.completion", "created": 1700000000,
  "model": "<model-name>",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "text"}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 48, "completion_tokens": 10, "total_tokens": 58}
}
```

Streaming chunk shape:

```
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"token"},"finish_reason":null}]}

```

Final: `finish_reason:"stop"` + empty content, then `data: [DONE]\n\n`.

### Simplified /predict (optional)

Accepts `{"prompt": "...", "max_tokens": N}`; returns `{"text": "...", "usage": {...}}`. Useful for smoke tests.

### Semantics that matter

- **EOS handling**: do **not** emit the EOS token in `text` / `content`. End with `finish_reason: "stop"`.
- **Stop-string truncation**: truncate output **before** the stop string; don't emit it.
- **Usage accounting**: `completion_tokens` counts emitted tokens only (after EOS removal and stop truncation).
- **Model-ID tolerance**: accept any `model` field value; many benchmark clients pass a fixed hint like `"whisper"` or `"tts"`.

---

## Image generation (diffusion)

### POST /v1/images/generations

Request (JSON):

| Field | Type | Required | Default | Constraints | Notes |
|:------|:-----|:---------|:--------|:------------|:------|
| `prompt` | string | **yes** | — | max 32000 chars | |
| `model` | string | optional | server's | — | |
| `n` | integer | optional | 1 | 1–10 | number of images |
| `size` | string | optional | `"1024x1024"` | `256x256`, `512x512`, `1024x1024`, `1024x1536`, `1536x1024` | |
| `quality` | string | optional | `"auto"` | `standard`, `hd`, `auto` | |
| `response_format` | string | optional | `"b64_json"` | `b64_json`, `url` | |
| `style` | string | optional | `"vivid"` | `vivid`, `natural` | |
| `user` | string | optional | — | — | |

Response (`b64_json`):

```json
{
  "created": 1700000000,
  "data": [{"b64_json": "<base64-png>", "revised_prompt": "..."}],
  "usage": {"prompt_tokens": 12, "completion_tokens": 0, "total_tokens": 12}
}
```

Response (`url`):

```json
{
  "created": 1700000000,
  "data": [{"url": "http://localhost:8000/images/<image_id>.png", "revised_prompt": "..."}]
}
```

When returning URLs, serve images through a static file route (e.g. `/images/{filename}`).

---

## Text-to-speech

### POST /v1/audio/speech

Returns **raw audio bytes**, not JSON.

Request (JSON):

| Field | Type | Required | Default | Constraints | Notes |
|:------|:-----|:---------|:--------|:------------|:------|
| `model` | string | **yes** | — | — | |
| `input` | string | **yes** | — | max 4096 chars | text to synthesize |
| `voice` | string | **yes** | — | `alloy`, `echo`, `nova`, `shimmer` | map to speaker embedding / style |
| `response_format` | string | optional | `"mp3"` | `mp3`, `opus`, `aac`, `flac`, `wav`, `pcm` | |
| `speed` | number | optional | 1.0 | 0.25–4.0 | playback speed |
| `instructions` | string | optional | — | — | style / prosody hint |

Response (HTTP 200): raw audio binary stream.

Content-Type by format:

| `response_format` | Content-Type |
|:------------------|:-------------|
| `mp3` | `audio/mpeg` |
| `opus` | `audio/opus` |
| `aac` | `audio/aac` |
| `flac` | `audio/flac` |
| `wav` | `audio/wav` |
| `pcm` | `audio/pcm` — 16-bit signed LE, 24 kHz, mono |

Use `StreamingResponse` for streaming playback (chunk as audio is synthesized).

---

## Speech-to-text (ASR)

### POST /v1/audio/transcriptions

Uses **`multipart/form-data`**, not JSON.

Request fields:

| Field | Type | Required | Default | Constraints | Notes |
|:------|:-----|:---------|:--------|:------------|:------|
| `file` | binary | **yes** | — | mp3, mp4, mpeg, mpga, m4a, ogg, wav, webm, flac | audio file |
| `model` | string | **yes** | — | — | |
| `language` | string | optional | — | ISO-639-1 (`en`, `ja`, …) | hint |
| `prompt` | string | optional | — | — | context / style guidance |
| `response_format` | string | optional | `"json"` | `json`, `text`, `srt`, `verbose_json`, `vtt` | |
| `temperature` | number | optional | 0 | 0.0–1.0 | |
| `timestamp_granularities[]` | array | optional | `["segment"]` | `segment`, `word` | verbose_json only |
| `stream` | boolean | optional | false | — | SSE |

Response shapes:

| `response_format` | Shape |
|:------------------|:------|
| `json` | `{"text": "..."}` |
| `verbose_json` | adds `language`, `duration`, `segments[]`, `words[]` |
| `text` | plain text (not JSON) |
| `srt` / `vtt` | subtitle format (not JSON) |

Streaming (SSE):

```
data: {"type":"transcript.text.delta","delta":"partial "}

data: {"type":"transcript.text.done","text":"full."}

data: [DONE]

```

---

## Video generation

Video generation is **async**: submit a job, poll for status, download when complete.

### POST /v1/videos

Submit a job. Returns immediately.

Request (JSON):

| Field | Type | Required | Default | Constraints | Notes |
|:------|:-----|:---------|:--------|:------------|:------|
| `prompt` | string | **yes** | — | — | |
| `model` | string | optional | server's | — | |
| `size` | string | optional | `"1280x720"` | `1280x720`, `1920x1080`, `1080x1920`, `720x1280` | |
| `seconds` | integer | optional | 4 | — | duration |
| `n` | integer | optional | 1 | — | number of videos |
| `user` | string | optional | — | — | |

Response:

```json
{
  "id": "video_<hex>", "object": "video", "created_at": 1700000000,
  "status": "queued", "model": "...", "progress": 0,
  "seconds": 4, "size": "1280x720"
}
```

### GET /v1/videos/{video_id}

Poll status. `status` ∈ `queued`, `in_progress`, `completed`, `failed`. `progress` ∈ 0–100.

### GET /v1/videos/{video_id}/content

Download result. Query param `variant`:

| `variant` | Content-Type |
|:----------|:-------------|
| `video` (default) | `video/mp4` |
| `thumbnail` | `image/png` |

Returns raw binary. HTTP 404 if the job hasn't completed.

### DELETE /v1/videos/{video_id}

Delete job. Response: `{"id": "...", "object": "video", "deleted": true}`.

---

## Realtime audio (WebSocket)

Bidirectional audio + text streaming, following the OpenAI Realtime API protocol.

### POST /v1/realtime/sessions

Create a session, return an ephemeral client token.

Request (JSON):

| Field | Type | Required | Default | Notes |
|:------|:-----|:---------|:--------|:------|
| `model` | string | optional | server's | |
| `modalities` | array | optional | `["text", "audio"]` | `text`, `audio` |
| `instructions` | string | optional | — | system instructions |
| `voice` | string | optional | `"alloy"` | voice for audio output |
| `input_audio_format` | string | optional | `"pcm16"` | `pcm16`, `g711_ulaw`, `g711_alaw` |
| `output_audio_format` | string | optional | `"pcm16"` | same options |
| `turn_detection` | object | optional | — | VAD config |
| `temperature` | number | optional | 0.8 | 0.6–1.2 |
| `max_response_output_tokens` | int or `"inf"` | optional | — | |

Response: session object including `client_secret: {value: "ek_...", expires_at: N}`.

### WebSocket /v1/realtime

Connect with `?model=<name>`. All messages are JSON. Audio payloads are base64-encoded. Default audio: **PCM16, 16-bit signed LE, 24 kHz, mono**.

#### Client → Server events

- `session.update`
- `input_audio_buffer.append` — `audio: "<base64>"`
- `input_audio_buffer.commit`
- `input_audio_buffer.clear`
- `conversation.item.create`
- `conversation.item.delete`
- `response.create`
- `response.cancel`

#### Server → Client events

- `session.created`, `session.updated`
- `input_audio_buffer.speech_started`, `speech_stopped`, `committed`
- `conversation.item.created`
- `response.created`
- `response.text.delta`, `response.text.done`
- `response.audio.delta`, `response.audio.done`
- `response.audio_transcript.delta`, `response.audio_transcript.done`
- `response.done`
- `error`

---

## Common serving-side gotchas

- **Model-ID field is advisory**. Accept any string — clients often send a hint (`"whisper"`, `"tts"`) rather than the full model name.
- **`/v1/models` id should reflect the actual model**, not the filesystem mount point. For HF checkpoints, use `config._name_or_path` from `config.json`.
- **Don't 404 on model mismatch**. The spec treats `model` as a hint, not a validator.
- **SSE chunks end with a blank line**. Each `data: {...}` must be followed by `\n\n`.
- **Binary responses** (TTS `/v1/audio/speech`, video `/v1/videos/*/content`) must set the correct `Content-Type`; OpenAI client SDKs rely on it.
- **WebSocket base64 is case-sensitive and requires correct padding**. Don't strip `=`.
- **Multipart vs JSON is easy to confuse**: `/v1/audio/transcriptions` is multipart (file upload), not JSON.
- **CORS**: clients that run in a browser need `Access-Control-Allow-Origin`. Add to the FastAPI app via `CORSMiddleware`.

## Minimum-viable endpoint table (by modality)

Use this to scope a new endpoint:

| Modality | Endpoints | Transport |
|:---------|:----------|:----------|
| Text | `/v1/completions`, `/v1/chat/completions`, `/v1/models`, `/health` | JSON + SSE |
| Image | `/v1/images/generations`, `/v1/models`, `/health` | JSON |
| TTS | `/v1/audio/speech`, `/v1/models`, `/health` | JSON in, raw audio out |
| ASR | `/v1/audio/transcriptions`, `/v1/models`, `/health` | multipart in, JSON/text/SRT/VTT out (+SSE) |
| Video | `/v1/videos` (POST/GET/DELETE), `/v1/videos/{id}/content`, `/v1/models`, `/health` | async JSON + raw binary |
| Realtime | `/v1/realtime/sessions`, WebSocket `/v1/realtime`, `/v1/models`, `/health` | WebSocket JSON + base64 audio |

## See also

- [`tooling/fastapi-serving/`](fastapi-serving.md) — FastAPI + lifespan + async patterns for the endpoints above (focus is text but the async-lock / streaming recipes transfer)
- [`tooling/io-handling/`](io-handling.md) — tokenization, chat templates, multimodal preprocessing, incremental UTF-8 detokenization, SSE formatting
- [`models/*`](../../models/) — each modality's model-architecture side
