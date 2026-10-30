# FastAPI Model Serving

Build a self-contained FastAPI server that serves a HuggingFace causal LM with an OpenAI-compatible API.

## Workflow

1. Load `.env` for `HF_TOKEN` (parse manually — no dependency on python-dotenv)
2. Load model and tokenizer in FastAPI lifespan context
3. Expose endpoints: `/health`, `/v1/models`, `/v1/completions`, `/v1/chat/completions`
4. For each request, serialize GPU access with `asyncio.Lock` and offload to `asyncio.to_thread`

## Two Generation Paths

### Non-streaming: `model.generate()`

Use HuggingFace's `model.generate()` directly. This is the simplest and most correct path.

```python
output_ids = model.generate(**inputs, max_new_tokens=N, do_sample=temperature > 0, temperature=temperature, top_p=top_p)
new_ids = output_ids[0, prompt_len:]
text = tokenizer.decode(new_ids, skip_special_tokens=True)
```

### Streaming: manual token-by-token loop with KV cache

For SSE streaming, run a manual loop that yields one token at a time:

```python
past_key_values = None
for _ in range(max_new_tokens):
    model_inputs = {"input_ids": generated_ids if past_key_values is None else generated_ids[:, -1:]}
    model_inputs["past_key_values"] = past_key_values
    model_inputs["use_cache"] = True

    outputs = model(**model_inputs)
    past_key_values = outputs.past_key_values
    next_token = outputs.logits[:, -1, :].argmax(dim=-1, keepdim=True)  # greedy
    # ... yield token, check EOS, append to generated_ids
```

## Critical Implementation Details

- **HF token auth**: Pass `token=` to `AutoTokenizer.from_pretrained()` and `AutoModelForCausalLM.from_pretrained()`. Do NOT call `hf_login()` — it tries to write to a shared filesystem and can fail with `PermissionError`.
- **Pad token fallback**: `if tokenizer.pad_token is None: tokenizer.pad_token = tokenizer.eos_token`
- **Chat template**: Use `tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)` when the tokenizer has a chat template. Fall back to `"role: content\n..."` format otherwise.
- **Async safety**: Wrap blocking GPU calls in `asyncio.to_thread()`. Use a single `asyncio.Lock` to serialize model access (the model is not thread-safe).
- **SSE format**: Each chunk is `data: {json}\n\n`. Final messages: a chunk with `finish_reason: "stop"`, then `data: [DONE]\n\n`.

## Response Schemas



---

## Openai Api Spec

## POST /v1/completions (non-streaming)

```json
{
  "id": "cmpl-<random_hex>",
  "object": "text_completion",
  "created": 1700000000,
  "model": "meta-llama/Llama-3.2-1B-Instruct",
  "choices": [
    {
      "text": "generated text here",
      "index": 0,
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 6,
    "completion_tokens": 20,
    "total_tokens": 26
  }
}
```

## POST /v1/chat/completions (non-streaming)

```json
{
  "id": "chatcmpl-<random_hex>",
  "object": "chat.completion",
  "created": 1700000000,
  "model": "meta-llama/Llama-3.2-1B-Instruct",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "generated text here"},
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 48,
    "completion_tokens": 10,
    "total_tokens": 58
  }
}
```

## Streaming chunks (SSE)

### Completion stream chunk

```
data: {"id": "cmpl-xxx", "object": "text_completion", "created": 1700000000, "model": "...", "choices": [{"text": "token", "index": 0, "finish_reason": null}]}
```

### Chat stream chunk

```
data: {"id": "chatcmpl-xxx", "object": "chat.completion.chunk", "created": 1700000000, "model": "...", "choices": [{"index": 0, "delta": {"content": "token"}, "finish_reason": null}]}
```

### Final chunk (both types)

A chunk with `"finish_reason": "stop"` and empty content (`"text": ""` or `"delta": {}`), followed by:

```
data: [DONE]
```

## Request schemas

### CompletionRequest

| Field       | Type                    | Default | Constraints      |
|-------------|-------------------------|---------|------------------|
| model       | str \| None             | None    |                  |
| prompt      | str \| list[str]        | required|                  |
| max_tokens  | int                     | 256     | 1-4096           |
| temperature | float                   | 1.0     | 0.0-2.0          |
| top_p       | float                   | 1.0     | 0.0-1.0          |
| stop        | str \| list[str] \| None| None    |                  |
| stream      | bool                    | false   |                  |

### ChatCompletionRequest

| Field       | Type                    | Default | Constraints      |
|-------------|-------------------------|---------|------------------|
| model       | str \| None             | None    |                  |
| messages    | list[ChatMessage]       | required|                  |
| max_tokens  | int                     | 256     | 1-4096           |
| temperature | float                   | 1.0     | 0.0-2.0          |
| top_p       | float                   | 1.0     | 0.0-1.0          |
| stop        | str \| list[str] \| None| None    |                  |
| stream      | bool                    | false   |                  |

### ChatMessage

| Field   | Type | Description          |
|---------|------|----------------------|
| role    | str  | "system", "user", or "assistant" |
| content | str  | Message content      |
