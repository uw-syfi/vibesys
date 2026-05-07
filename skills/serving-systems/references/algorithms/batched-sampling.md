# Batched sampling

Sample one token per active request per step, with per-request parameters, in a single kernel pipeline. The goal: one `.tolist()` per step instead of one `.item()` per request.

## Why it matters

The naive per-request loop:

```python
for req in batch:
    token = sample(logits[req], req.temperature, req.top_p, ...)
    ids[req] = token.item()   # CPU sync
```

At batch=32, decode=10ms, that's 32 syncs per step — typically 10–30% of per-step wall time. Batched sampling does the same work in one kernel with one sync.

## Parameter vectorization

Each request has its own `(temperature, top_p, top_k, min_p, penalties, seed)`. Represent them as per-request tensors on device:

```python
temperatures = torch.tensor([r.temperature for r in reqs], device="cuda")
top_ps       = torch.tensor([r.top_p       for r in reqs], device="cuda")
# ... etc
```

`r.temperature == 0` (greedy) is handled by argmax + mask, not by dividing by near-zero.

## Pipeline

```
logits
  ↓ penalties (repetition, presence, frequency; needs last-N context)
  ↓ logits_bias (static per-request, e.g., block token ids)
  ↓ structured_output_mask (if any)
  ↓ softmax(/temperature)
  ↓ top-k
  ↓ top-p (cumulative)
  ↓ min-p
  ↓ sampling
sampled_tokens
```

Order matters: temperature before top-p (which operates on probs); penalties before temperature (which operates on logits).

## Joint filter with rejection sampling

Applying top-p, top-k, and min-p together via mask-then-sample requires sorting or scanning twice. FlashInfer's `top_p_sampling_from_probs` uses a **rejection sampling** approach: sample from the unrestricted distribution, check filter, resample on reject — in practice ~1–2 attempts per token. Same distribution, much faster.

## Logits-processor pipelines

Engines support pluggable per-request logits processors (for watermarking, rep penalty variants, custom biases). These run on device before the sampling kernel. Keep each processor vectorized; a Python loop here eats all the sampling gains.

## Compatibility

| Kernel / library | Engines | Notes |
|:-----------------|:--------|:------|
| `flashinfer.top_p_sampling_from_probs` | vLLM, SGLang | rejection-sampling kernel |
| `flashinfer.softmax` (temperature-aware) | vLLM, SGLang | single-pass, per-request temperature |
| vLLM v1 sampler | vLLM | fused path for batched decode |
| SGLang sampling | SGLang | per-request param tensors |
| TRT-LLM C++ sampler | TRT-LLM | in the batch manager |

## Engine pointers

| Engine | Sampling path |
|:-------|:--------------|
| vLLM | `vllm/v1/sample/sampler.py` + `vllm/v1/sample/logits_processor/`, `vllm/v1/sample/rejection_sampler.py` (spec decode verify) |
| SGLang | `python/sglang/srt/layers/sampler.py`, `python/sglang/srt/sampling/` |
| TRT-LLM | C++ side `cpp/tensorrt_llm/runtime/` (gptDecoder, decoderState), Python `_torch/speculative/spec_sampler_base.py` |

## Interaction with other features

- **Speculative verify**: uses per-position rejection sampling (different from top-p rejection — here it enforces correctness of target's distribution). Same kernel family.
- **Structured output**: mask applies before top-p/k.
- **Logprobs request**: if a caller wants `logprobs=True`, capture top-k probs before the sample kernel, pass through to response.
- **Beam search**: different beast — serving engines usually don't do it. If needed, keep it out of the hot path.

## CUDA-graph compatibility

- **Flashinfer samplers: graph-compatible** (regular CUDA kernels, fixed shapes).
- **Per-request parameter tensors: fine** (stable shape = `max_batch`, pad unused slots).
- **Random seed per request**: pre-generate PRNG state per step in a captured buffer.
- **`.tolist()` at the end**: the one sync is outside the graph replay, which is OK.

## Pitfalls

- **Temperature=0 with naive softmax** produces NaNs. Gate with argmax + mask on the temperature vector.
- **Top-k with k > vocab**: treat as no-op; some kernels error.
- **Repetition penalty over wrong window**: spec says "over all prior tokens," many implementations use a small window. Document behavior.
- **Seed handling across CUDA-graph replay**: the same graph replayed twice with the same seed produces the same sample — harmless if intended, subtle bug if not.
- **Partial batch (some slots inactive)**: padded slots must not contribute valid tokens to the output; either mask their logits to -inf or slice the output.
- **Sampling with FP8/FP4 logits**: cast to FP32 before softmax to avoid underflow.

## See also

- `backends/flashinfer/` — fused sampler kernels
- `algorithms/async-scheduling/` — pushes the remaining sampling sync (copy_event.synchronize) off the hot path
- `algorithms/structured-output/`
- `algorithms/speculative-decoding/` — rejection-sample verify uses the same machinery
- `backends/cuda-graph/`
