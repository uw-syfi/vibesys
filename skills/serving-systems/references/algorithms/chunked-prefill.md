# Chunked prefill

Prefill a long prompt 512–8192 tokens at a time, co-batched with active decodes, instead of running the whole prefill as a single long forward. Smooths TPOT under concurrency, at a small cost to the prefilling request's own TTFT.

## Why

A single 32k-token prefill forward on a 70B model can take hundreds of milliseconds. If active decodes have to wait for it, their per-token latency (TPOT) spikes for that whole window. Chunked prefill caps the compute per forward step at roughly `chunk_size × decode_batch_size` compute units.

## Concept

- **Token budget per step**: `max_num_batched_tokens` (or equivalent). Scheduler fills this budget with (a) one partial prefill chunk + (b) as many decode tokens as fit.
- **Partial prefill**: a prefill request produces no output token until its last chunk completes — earlier chunks only populate KV cache.
- **Attention mask**: mixed-batch mask is block-diagonal with a causal block for the prefill chunk and single-row blocks for decodes. Paged-attention kernels with variable-length queries (FlashInfer `BatchPrefillWithPagedKVCacheWrapper`, FA2/FA3 `flash_attn_varlen_func`) handle this natively.
- **Last chunk**: produces the first output token; the request transitions to decode.

## Chunk-size selection

Tradeoff:

| Smaller chunks | Larger chunks |
|:---------------|:--------------|
| Lower TPOT inflation per step | Faster TTFT for the prefilling request |
| More scheduler overhead | Fewer wakeups |
| Better concurrency under mixed load | Closer to unchunked behavior |

Common starting points: 512 tokens for aggressive latency, 2048–4096 as a balanced default, 8192+ when TTFT dominates the SLA.

## Compatibility

| Implementation | Engine | Enabled by | Notes |
|:---------------|:-------|:-----------|:------|
| Sarathi-style chunked prefill | vLLM v1 | on by default | `vllm/v1/core/sched/scheduler.py` budgets via `max_num_batched_tokens` |
| SGLang chunked prefill | SGLang | on by default | `python/sglang/srt/managers/scheduler.py` |
| Inflight batching + chunked context | TensorRT-LLM | PyExecutor / C++ batch manager | see batch manager scheduler |

## Engine pointers

| Engine | Scheduler / budget | Attention handling |
|:-------|:-------------------|:-------------------|
| vLLM | `vllm/v1/core/sched/scheduler.py`, `vllm/v1/core/sched/request_queue.py` | `BatchPrefillWithPagedKVCacheWrapper` (flashinfer backend) or `flash_attn_varlen_func` (FA backend) |
| SGLang | `python/sglang/srt/managers/scheduler.py` (look for `get_new_batch_prefill`) | same attention wrappers |
| TensorRT-LLM | C++ `trtGptModelInflightBatching.cpp`, Python `_torch/pyexecutor/py_executor.py` | `_torch/attention_backend/` |

## Pitfalls

- **CUDA-graph capture assumes fixed shape.** Chunked-prefill forwards have variable total-token counts; capture decode-only and keep prefill eager, or capture a small ladder of prefill shapes.
- **Position IDs across chunks.** Chunk 2 starts at `past_len = len(chunk_1)`; bookkeep per request.
- **Sampling on the last chunk only.** Non-terminal chunks produce logits that must be discarded (or not computed if the kernel allows).
- **Quantized activations with small chunks.** Per-tensor activation scales calibrated on full prefill may over/underflow on small chunks; per-token scales avoid this.
- **Benchmark correctness**: TTFT of the chunked request includes its N chunks. Comparing chunked vs un-chunked TTFT without fixing prompt length is apples-to-oranges.

## See also

- `algorithms/continuous-batching/` — prerequisite
- `algorithms/paged-attention/` — paged KV makes mixed batches efficient
- `backends/cuda-graph/` — capture strategy for mixed batches
- `tooling/serving-benchmark/` — TTFT vs TPOT measurement
