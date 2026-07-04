Maximize **single-batch end-to-end completion throughput** (median completion tokens per second) for **code-debug** edits driven by `bench/benchmark.py`. The target model is `Qwen/Qwen3-32B`. Implement an OpenAI-style **predicted-outputs** server: each request body carries a `prediction.content` field (the buggy original code), and the server consumes that prediction as the draft sequence in a speculative-decoding loop against the target.

**Headline metric**: `median_tok_per_sec` from `bench/benchmark.py`'s JSON output ("Primary metric: median_tok_per_sec = ..."). Single-batch (concurrency 1), tokens counted by re-tokenizing the concatenated server response. This is the only number `perf_metric` should record.

## Server contract

- Streaming `POST /v1/completions`. Bench sends `prompt_is_preformatted: True` — tokenize the prompt verbatim, no second chat template pass.
- Accept the predicted-outputs envelope `{"prediction": {"type": "content", "content": "<text>"}}`. Tokenize `prediction.content` once at request-time; it is a draft hint, **not** part of the prompt.
- Disable thinking mode (`enable_thinking=False`); Qwen3's `<think>...</think>` would blow past `max_tokens`.
- Capture prefill/decode/verify-extend in CUDA graphs. Default attention: FlashInfer batched wrappers with `use_cuda_graph=True`.

## Speculation algorithm

Stateless n-gram lookup (prompt-lookup decoding). The drafter is the prediction itself — no drafter model, no cursor between cycles. Each cycle: tokenize `prediction.content` once; take the last 2-8 emitted tokens as a needle and find the rightmost occurrence in the prediction; propose the next K tokens after the match as the draft; verify in one forward over `[seed] + draft` through the captured graph; accept the longest matching prefix and emit prefix + bonus token, which seeds the next lookup. One verify forward per cycle — no separate decode pass. On no match (including the very first cycle), emit one AR token and retry. Use a single K=16 verify graph throughout.

## Expected throughput

K=16 + speculation on this workload (90–95 % per-token match) should land in **~200–400 median tok/s** on H100 bf16 vs ~30 tok/s vanilla AR. 
