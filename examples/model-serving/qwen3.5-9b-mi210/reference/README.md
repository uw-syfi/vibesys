# Reference engine: Qwen3.5-9B on 1x MI210

Minimal, correct, deliberately unoptimized. Serves the text path of
`Qwen/Qwen3.5-9B` (bf16) behind an OpenAI-compatible `/v1/completions`.

## Launch

From the bundle root (`examples/model-serving/qwen3.5-9b-mi210/`):

```bash
HF_HUB_OFFLINE=1 python -m reference.server --model Qwen/Qwen3.5-9B --host 0.0.0.0 --port 8000
```

| Option | Default | Meaning |
|:--|:--|:--|
| `--model` | required | HF repo id (resolved from the local HF cache, no network) or a local checkpoint dir |
| `--served-model-name` | `--model` | id listed by `/v1/models` and accepted in requests |
| `--max-model-len` | 32768 | max prompt + generated tokens per request (longer requests get 400) |
| `--device` | `cuda` | torch device |
| `--log-level` | `info` | |

Requirements: torch (ROCm), transformers >= 5.x (config/tokenizer only),
safetensors, huggingface_hub, fastapi, uvicorn. The server loads weights and
runs a 2-token warmup before binding the port, so `GET /health` answering 200
means ready.

## HTTP surface

- `GET /health`: 200 when the GPU worker is alive.
- `GET /v1/models`: lists the served model id.
- `POST /v1/completions` (vLLM response shapes):
  - `prompt`: string, list of token ids, or a one-element list of either.
  - `max_tokens` (default 16), `temperature` (default 1.0; 0 = greedy),
    `top_p`, `seed`, `n` (only 1), `stop` (not supported; must be empty).
  - vLLM extensions: `ignore_eos`, `min_tokens`, `return_token_ids`
    (`choices[].token_ids`, plus `prompt_token_ids` on the first stream chunk),
    `return_tokens_as_token_ids`.
  - `logprobs` (0..20) for generated tokens; `echo` + `logprobs` also returns
    prompt-token logprobs (non-streaming only).
  - `stream` (SSE `data:` chunks, one per generated token, ending with
    `data: [DONE]`); `stream_options.include_usage` adds a final usage chunk
    (`choices: []`).
  - `usage`: `prompt_tokens`, `completion_tokens`, `total_tokens`,
    `prompt_tokens_details.cached_tokens` (always 0: no prefix caching).
  - Unknown request keys are ignored (session_runner sends `rid`).
- Stop tokens: `<|endoftext|>` (config `eos_token_id`) and `<|im_end|>`
  (tokenizer EOS), unless `ignore_eos`.

## Structure

| File | Role |
|:--|:--|
| `config.py` | typed `TextConfig` from the checkpoint's `text_config` |
| `weights.py` | strict safetensors load of `model.language_model.*` + `lm_head`; vision tower and MTP head skipped |
| `model.py` | explicit layers: zero-centered RMSNorm, gated GQA attention (q/k norm, partial RoPE, sigmoid output gate), Gated DeltaNet (short conv, decay/beta gates, fp32 delta rule, gated RMSNorm), SwiGLU MLP; per-sequence `SequenceState` |
| `engine.py` | `Engine.generate` (prefill + decode loop, one request) and `Engine.score` (teacher-forced logprobs) |
| `server.py` | FastAPI app; one GPU worker thread runs requests FIFO |

Model facts (from `config.json`): 32 layers, `full_attention_interval` 4
(layers 3, 7, ..., 31 are full attention, 24 GDN layers), hidden 4096,
full attention 16 q heads / 4 kv heads, head_dim 256, rotary on the first 64
dims (theta 1e7); GDN 16 k heads / 32 v heads, head dim 128, conv kernel 4;
vocab 248320, untied LM head. The checkpoint's M-RoPE collapses to 1-D RoPE for
text-only input.

Numerics mirror HF `modeling_qwen3_5.py` (transformers 5.17), including where
it casts: norms in fp32, rotary tables cast to bf16, GDN recurrence and state
in fp32, everything else bf16.

## Where optimizations slot in

Named in code comments; none is implemented here.

- Batching/scheduling: `Engine.generate` handles one sequence; `Worker` in
  `server.py` is strictly FIFO. Continuous batching replaces both with a step
  loop over many sequences.
- Attention KV: `AttentionCache` is a contiguous per-request buffer; paged KV
  and a fused decode/prefill attention kernel replace it and the SDPA call.
- GDN: `chunked_gated_delta_rule` (prefill) and `recurrent_gated_delta_rule`
  (decode) are torch ports of HF's fallbacks; fla's `chunk_gated_delta_rule` /
  `fused_recurrent_gated_delta_rule` and causal-conv1d kernels are the drop-in
  fused equivalents. Conv/recurrent state would move into a slot pool.
- Launch overhead: decode is launch-bound (~20 tok/s vs a ~66 tok/s HBM
  roofline); HIP graphs and fusions (norm+gate, qkv split, sampling) target it.

## Measured (rough data point, not a baseline)

MI210, 2026-09-23. Request Factory `session_runner --backend openai`,
independent trace of 24 requests (input 128 to 2048, output 64 to 256 tokens),
`--arrival-mode saturated --max-concurrency 4`: 24/24 succeeded, every request
got its exact target output length, usage and `token_ids` parsed.

| Metric | Value |
|:--|--:|
| TPOT mean / p50 | 51.0 / 49.6 ms (~20 tok/s single stream) |
| TTFT mean / max (includes FIFO queueing behind 3 other requests) | 20.5 / 44.2 s |
| Output throughput | 18.9 tok/s |
| Prefill, 2000 tokens, alone | 0.63 s |
| Weight load / time to healthy | 35 s / ~42 s |

These numbers only show where the reference sits (vLLM decodes ~66 tok/s
single-stream on this GPU); `benchmark/run.py` is the measurement of record.
