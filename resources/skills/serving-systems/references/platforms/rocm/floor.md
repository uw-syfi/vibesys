# ROCm (AMD Instinct) optimization floor

CDNA shares the discrete-accelerator model with CUDA — separate device memory, dynamic shapes, per-kernel launch cost — so the *shape* of the floor matches NVIDIA's even though the libraries differ. Where a technique is identical apart from the library name, this file says so rather than restating it.

> **Status: experimental.** The `rocm` backend is wired but has not been exercised end to end against MI300-class hardware in this repo. Treat kernel-library specifics here as directionally correct and verify against your ROCm version before relying on them.

## 1. Continuous batching

Identical to CUDA in both design and rationale — dynamic shapes are cheap, so eliminate padding via variable-length packing or paged KV.

- Contract: [`algorithms/continuous-batching.md`](../../algorithms/continuous-batching.md)
- The CUDA implementation transfers directly; substitute the attention kernel below.

## 2. Fused attention kernel

Never run naive `softmax(QKᵀ)V`. On CDNA the options are:

| Option | When |
|:--|:--|
| **AITER** | AMD's fused-op library; the closest analog to FlashInfer. First choice where it covers the variant. |
| **Composable Kernel (CK)** | One of the layers AITER is built from (alongside Triton, HIP, and ASM); use directly for variants AITER doesn't cover. |
| **Triton** | Portable fallback; runs on CDNA and is how vLLM/SGLang cover ROCm gaps. See [`frameworks/triton.md`](../../frameworks/triton.md). |
| **PyTorch SDPA** | Floor. Fused, available, slower than the above. |

- [`aiter.md`](aiter.md)

## 3. HIP graphs

The launch-overhead problem and its remedy are the same as CUDA's; the API is `hipGraph`. `torch.compile(mode="reduce-overhead")` drives it through the same PyTorch path used on NVIDIA.

Capture decode, keep prefill eager or bucketed. The shape-stability and address-stability requirements carry over unchanged.

## Then

1. **Paged KV** — [`algorithms/paged-attention.md`](../../algorithms/paged-attention.md); the design applies unmodified.
2. **Prefix caching** — [`algorithms/radix-prefix-caching.md`](../../algorithms/radix-prefix-caching.md).
3. **Chunked prefill** — [`algorithms/chunked-prefill.md`](../../algorithms/chunked-prefill.md).
4. **Quantization** — FP8 is native from MI300 (CDNA3); FP4 from CDNA4. See [`algorithms/quantization-schemes.md`](../../algorithms/quantization-schemes.md) and the HW floor in [`hardware.md`](hardware.md).

## Where ROCm differs from CUDA

| Concern | Difference |
|:--|:--|
| Interconnect | Infinity Fabric, not NVLink. Lower per-pair bandwidth; no NVL72-equivalent domain. Affects TP sizing — see [`algorithms/parallelism.md`](../../algorithms/parallelism.md). |
| Memory capacity | MI300X ships 192 GB and MI325X 256 GB — larger than contemporary NVIDIA parts. Capacity-bound designs that need offload on NVIDIA may fit resident here. |
| Kernel coverage | Narrower than NVIDIA. Verify the specific attention variant and quantization scheme are implemented before designing around them. |
| Engine support | vLLM and SGLang support ROCm; TensorRT-LLM does not. |

## See also

- [`hardware.md`](hardware.md) — MI300X / MI325X / MI350X specs, GFX IDs, precision support
- [`aiter.md`](aiter.md) — AITER / Composable Kernel
- [`profiler.md`](profiler.md) — rocprofv3 / rocprof-compute
- [`roofline.md`](roofline.md): per-arch compute/bandwidth ceilings, including this repo's measured MI210 sustained ceiling
