# #0001 — Build FastAPI inference server for the reference model

- **Type**: feature
- **Status**: closed
- **Attempts**: 1
- **Created by**: loop:bootstrap (iter 1)
- **Created at**: <TIMESTAMP>
- **Updated at**: <TIMESTAMP>
- **Closed at iter**: 1

## Description

## Background

Build a production-ready FastAPI inference server that serves **the specific model defined in the reference implementation** at `.`. Read the reference code and any config files carefully — your server must implement this exact model architecture and its weights loading. Do NOT build a generic wrapper around the `transformers` library (e.g. `AutoModelForCausalLM`). Instead, write self-contained model code that directly implements the layers, attention, MLP, etc. using PyTorch, based on the reference.

**Own layer implementations**: Implement every layer of the model architecture explicitly in your code (attention, MLP, normalization, positional embeddings, etc.). You may use `transformers` as a utility (e.g. `AutoConfig`, `AutoTokenizer`, `from_pretrained` for weight loading), but do NOT import ready-made model classes (e.g. `LlamaModel`, `LlamaAttention`). Each layer must be defined in your own code so it can be optimized in later steps.

**Model weights**: The model weights directory is at the fixed path `/model` inside the workspace. Do NOT download or fetch models from the internet.

**GPU and dtype**: Load the model on GPU (`device="cuda"`) with an efficient dtype (`torch.bfloat16` or `torch.float16`). Do NOT use `float32` or CPU — inference will be far too slow.

**Environment**: Initialize the project with `uv init --no-vcs` and add dependencies via `uv add`. Always use `uv run` to execute scripts and tests.

**VibeServeModel interface**: Inspect the accuracy checker and preserve the entry
module it imports. That module must export a class named `VibeServeModel`; the
production server may use a different language or runtime behind this adapter.
The class must implement:

1. `model = VibeServeModel.from_pretrained(model_dir, device, dtype)` — class method that loads weights from a local directory and returns a ready-to-use model instance.
2. `output_ids = model.generate(input_ids, max_new_tokens=N)` — greedy generation that returns a tensor of shape `(1, prompt_len + generated_len)` (same convention as HuggingFace `model.generate()`).

Keep this interface working across all issues, even as internals change.

**Streaming token visibility (CRITICAL)**: The `/v1/completions` SSE streaming endpoint MUST emit a **non-empty `text` delta for every generated token**. The benchmark measures token throughput, TTFT (time to first token), and TPOT (time per output token) by counting non-empty SSE chunks. If chunks have empty text, the benchmark will report `token_throughput = 0` and `ttft_ms`/`tpot_ms = null`, which means the server's performance cannot be measured or improved.

Common causes of empty token deltas:

- Calling `tokenizer.decode(token_id, skip_special_tokens=True)` on a single token that produces an empty string (e.g. byte-level BPE fragments, SentencePiece leading spaces). **You must handle this** — fall back to the raw token string, a placeholder character, or incremental detokenization.
- Materializing all tokens first and then emitting SSE chunks (defeats streaming — emit each token as it is generated).
- Accidentally hitting a fallback/synthetic completion path instead of real model inference.

This is the foundation issue. Subsequent perf, feature, and bug issues filed by the perf evaluator and judge will refine this implementation over time.

## Acceptance criteria

- The entry module imported by the accuracy checker exports a `VibeServeModel` class with the `from_pretrained` / `generate` interface described above.
- A FastAPI server exposes both `/v1/completions` (SSE streaming) and `/health` endpoints.
- The model loads on GPU with `bfloat16`/`float16` from `/model` — no `float32`, no CPU, no internet fetches.
- Every layer of the model architecture is implemented in the workspace code (no `LlamaModel` / `LlamaAttention` imports).
- The project is initialized with `uv init --no-vcs`, dependencies added via `uv add`, and scripts run through `uv run`.
- The `/v1/completions` SSE stream emits a non-empty `text` delta per generated token, and the benchmark reports `token_throughput > 0` with non-null `ttft_ms` / `tpot_ms`.
- The accuracy checker command `python -c 'print('"'"'ok'"'"')'` passes when run against `VibeServeModel`.
- The benchmark command `python -c 'print('"'"'ok'"'"')'` completes at least one request and reports `token_throughput > 0`.
- A pytest test suite covers `/health`, `/v1/completions`, the accuracy checker, and a benchmark sanity run.
- All tests pass via `uv run pytest -v`.

## Notes

This is the initial bootstrap issue auto-created by the issue-loop on the first iteration. Resolving it brings the workspace to "minimum viable serving"; performance optimizations and bug fixes will follow as separate issues filed by the perf evaluator and judge.

## Timeline

- `<TIMESTAMP>` **loop:bootstrap** create (iter 1)
- `<TIMESTAMP>` **loop** open->in_progress (iter 1) — claimed for processing
- `<TIMESTAMP>` **implementer** attempt (iter 1) — Built the inference server.
- `<TIMESTAMP>` **judge** in_progress->closed (iter 1) — closed by judge after attempt 1

## Attempt detail

### Implementer attempt 1 (iter 1)

**Summary**: Built the inference server.

**Files touched**:
- `server.py`

**Self-check**: ran the accuracy checker locally

### Judge review 1 (iter 1)

**Verdict**: PASS

**Analysis**: reviewed the diff and the accuracy checks
