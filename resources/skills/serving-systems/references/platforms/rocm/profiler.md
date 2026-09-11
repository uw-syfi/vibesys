# Profiling on ROCm

The workflow mirrors CUDA's — classify with a system timeline, then descend — so read [`tooling/profiler.md`](../../tooling/profiler.md) for the discipline and treat this as the tool substitution table.

`rocprofv3` (kernel-trace mode with PMC counters) has been exercised end to end for a kernel-internal MoE GEMM profile on MI300A; see the pitfall below for the working path and the one it replaced. Other tool/flag combinations in this file are not independently verified; check against your ROCm version.

## Tool substitutions

| Altitude | CUDA | ROCm |
|:--|:--|:--|
| System timeline | `nsys` | `rocprofv3` |
| Framework / op | torch profiler | torch profiler (works unmodified on ROCm) |
| Kernel-internal | `ncu` | `rocprof-compute` (named `omniperf` before ROCm 6.3) |

`torch.profiler` is the reason vibesys can use `ProfilerKind.TORCH` on this backend without a dedicated profiler kind — it works on ROCm as-is and covers the framework altitude.

## Notes

- **Start with `rocprofv3`** for a new problem, exactly as you would start with `nsys`. The classification it produces — host-bound, launch-bound, memory-bound, collective-bound — maps onto the same finding→fix table in the contract.
- **`rocprof-compute` has the same overhead warning as `ncu`.** Target specific kernels; do not profile a whole run at kernel altitude. The tool was renamed from `omniperf` in ROCm 6.3 (`rocprofiler-compute`); `omnitrace` likewise became `rocprof-sys` / `rocprofiler-systems`, which is the closer analog to `nsys` for a system timeline.
- **Collectives** show as RCCL rather than NCCL. The topology reasoning differs — Infinity Fabric, not NVLink — see [`floor.md`](floor.md) and [`algorithms/parallelism.md`](../../algorithms/parallelism.md).

## The characteristic ROCm finding

A kernel that is present and correct but markedly slower than its NVIDIA counterpart usually means a fallback path was taken — the specific attention variant or quantization scheme isn't covered by AITER/CK on this ROCm version and silently landed on Triton or SDPA. Confirm which kernel actually ran before concluding the hardware is the limit.

## Reading a torch-profiler trace on this image

Facts that help identify what a trace event is, on the `sglang-v0.5.18-rocm700` image with aiter bundled:

- GPU kernel events carry `cat == "kernel"` in the exported trace.
- User-code annotations (`record_function` ranges) are mirrored onto the GPU stream track as well as the CPU track, not only the CPU one.
- Dense GEMM kernels that fell through to the hipBLASLt/Tensile default path are named `Cijk_*`.
- AITER's all-reduce kernels are named `cross_device_reduce_1stage` / `cross_device_reduce_2stage`.

Scope: rocm, `sglang-v0.5.18-rocm700-mi30x` with aiter bundled. Status: verified. Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-11.

## Pitfalls

### rocprof-compute fails its own dependency check on this image; rocprofv3 works

```
Symptom: rocprof-compute exits during its startup dependency check
         (mismatched astunparse version; missing dash, kaleido, plotext,
         textual, pymongo packages) before it profiles anything.
Cause:   the sglang-v0.5.18-rocm700 image does not ship the full
         rocprof-compute dependency set.
Fix:     use rocprofv3 (1.0.0 on this image) directly for kernel-level
         work instead: kernel-trace mode plus a --pmc counter pass covers
         the same kernel-internal altitude. Requesting more PMC counters
         in one pass than the hardware's counter blocks can hold leaves
         the profiled process hanging rather than erroring, so wrap every
         --pmc invocation in a timeout and keep each pass to counters
         that fit in one hardware counter block.
Scope:   rocm, sglang-v0.5.18-rocm700-mi30x image.
Status:  verified. sglang-v0.5.18-rocm700-mi30x, 2026-09-11, job 631890.
```

## See also

- [`floor.md`](floor.md) — the optimization floor
- [`aiter.md`](aiter.md) — kernel coverage, which determines whether a fallback was taken
- [`hardware.md`](hardware.md) — bandwidth and precision by SKU
