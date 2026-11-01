# CPU targets for serving

What to establish about the target machine before designing. Unlike the accelerator platforms there is no small SKU matrix worth enumerating — the relevant facts are properties you query on the host.

## What to check

| Property | How | Why it matters |
|:--|:--|:--|
| Core count (physical vs logical) | `lscpu`, `sysctl -n hw.physicalcpu` | Thread pool sizing; SMT siblings are not extra cores for this workload |
| Widest ISA | `lscpu \| grep Flags`, `sysctl -a \| grep hw.optional` | AVX-512 / AMX / NEON / SVE — several-fold difference, set at build time |
| Memory bandwidth | vendor spec; `mbw`-style probe | Sets the decode roofline, same as HBM bandwidth on an accelerator |
| NUMA topology | `numactl --hardware`, `lscpu` | Cross-socket weight access is a per-token penalty |
| Cache sizes (L2/L3) | `lscpu` | Determines useful blocking for the GEMM path |

## Roofline

The bandwidth-bound estimate is the same arithmetic as on any other backend:

```
tokens/sec ≈ memory_bandwidth_bytes_per_sec / bytes_per_token
bytes_per_token ≈ model_size_bytes   (weight-streaming dominated, batch 1)
```

At batch 1 a CPU streams the full weight set per token, so quantization moves this bound almost linearly — the practical justification for [`floor.md`](floor.md) §2.

Batching amortizes weight reads across sequences, which is why the bound softens as batch grows; that is the CPU-specific reason to batch, distinct from the accelerator rationale.

## Rough positioning

A modern server CPU has memory bandwidth on the order of a hundred-plus GB/s across all channels populated — one to two orders of magnitude below contemporary HBM. Expect CPU serving to be viable for small and heavily-quantized models and for workloads where the win is in the code rather than the kernels, which is the case `ComputeBackend.CPU` exists to serve.

Populating all memory channels matters more than clock speed for this workload; a half-populated board can halve throughput with no other change.

## See also

- [`floor.md`](floor.md) — threading, quantization, ISA width
- [`profiler.md`](profiler.md) — `perf stat` counters that confirm which bound you are on
