# CUDA optimization floor

Three techniques every production serving system on NVIDIA ships with. Confirm all three are in place before pursuing workload-specific optimizations — a missing one is almost always a larger win than whatever you were about to tune.

## 1. Continuous batching

Requests join the running generation loop between decode steps. Skip only when the workload is single-batch by contract.

- Contract and invariants: [`algorithms/continuous-batching.md`](../../algorithms/continuous-batching.md)
- CUDA implementation: [`continuous-batching.md`](continuous-batching.md)

The CUDA-specific point: **eliminate padding.** Dynamic shapes cost nothing here, so pad-and-stack is a baseline to grow out of, via variable-length packing (FlashAttention) or paged KV (FlashInfer).

## 2. Fused attention kernel

Never run naive `softmax(QKᵀ)V` on NVIDIA. Pick one:

- Picker (workload → backend, per-feature matrix): [`attention-backend-comparison.md`](attention-backend-comparison.md)
- Deep usage: [`flashinfer.md`](flashinfer.md), [`flashattention.md`](flashattention.md), [`sdpa.md`](sdpa.md)

SDPA is the floor when the others don't apply; it is still a fused kernel. "No fused kernel" is not an option on this backend.

## 3. CUDA graphs

Eliminates per-kernel launch overhead. Required for low-latency single-batch and for high-throughput batched decode — at small batch the launch cost is a large fraction of step time.

- [`cuda-graph.md`](cuda-graph.md)

Capture the decode step; keep prefill eager or capture a ladder of shapes. Most integration bugs are shape or address stability violations, not capture failures.

## Then, in order of typical payoff

1. **Paged KV** — [`algorithms/paged-attention.md`](../../algorithms/paged-attention.md); prerequisite for prefix sharing.
2. **Prefix caching** — [`algorithms/radix-prefix-caching.md`](../../algorithms/radix-prefix-caching.md); dramatic on RAG / few-shot / agent workloads.
3. **Chunked prefill** — [`algorithms/chunked-prefill.md`](../../algorithms/chunked-prefill.md); smooths TPOT under mixed load.
4. **Async scheduling** — [`async-scheduling.md`](async-scheduling.md); once launch overhead is gone, the Python scheduler is next.
5. **Quantization** — [`algorithms/quantization-schemes.md`](../../algorithms/quantization-schemes.md); FP8 from Ada (sm_89) and Hopper, FP4 from Blackwell.

## Hardware floor

Feature availability is generation-gated — see [`hardware.md`](hardware.md). FP8 needs Ada (sm_89) or Hopper+, FP4 needs Blackwell, FA3 needs sm_90+. L40S/L4 are Ada and do have FP8 tensor cores — don't rule them out for quantized serving. Check the target GPU before designing around a precision.

## See also

- [`profiler.md`](profiler.md) — nsys first, always
- [`algorithms/`](../../algorithms/) — portable contracts these implement
