# Profiling on Apple Silicon

Instruments is the system-timeline tool. Read [`tooling/profiler.md`](../../tooling/profiler.md) first for the altitude discipline; this file covers only what is Metal/MLX-specific.

## Tools

| Altitude | Tool |
|:--|:--|
| System timeline | Instruments — Time Profiler for host, Metal System Trace for GPU |
| Framework / op | MLX's own metal trace (`mx.metal.start_capture` / `stop_capture`) |
| Kernel | Instruments GPU counters |

`ProfilerKind.MACOS_CPU` in vibesys drives the Instruments Time Profiler with a `sample` fallback, which covers the host altitude. GPU-side capture is currently a manual step.

## The two findings that matter

Almost all MLX serving problems are one of these:

**1. A forced synchronous materialization inside the decode loop.** Any `.item()`, `float()`, `print`, or Python-side comparison on an MLX array forces evaluation and serializes the pipeline. On a timeline this appears as regular host stalls at step boundaries.

Find them by looking for host blocks that recur exactly once per token. The fix is to keep the value as an array until the end of the step — the same discipline as avoiding per-request host sync on CUDA, arrived at from a different direction.

**2. Bandwidth saturation.** Decode on this backend is bandwidth-bound at batch 1. If the timeline shows the GPU busy and throughput still low, the answer is usually not a better kernel — it is fewer bytes per token, i.e. quantization ([`floor.md`](floor.md) §5).

Compute the roofline before optimizing: `tokens/sec ≈ memory_bandwidth / bytes_per_token`. The per-SKU bandwidth figure is in [`hardware.md`](hardware.md). If measured throughput is already near that bound, no kernel work will help.

## What the contract's table maps to

- "Gaps between kernels → graph capture" → **N/A.** There is no capture; `mx.compile` fusion is the nearest lever and it addresses graph-construction cost, not launch cost.
- "Gaps between steps → async scheduling" → check `mx.eval` / `mx.async_eval` placement, per [`algorithms/async-scheduling.md`](../../algorithms/async-scheduling.md).
- "Collective-bound" → **N/A.** Single SoC.

## See also

- [`floor.md`](floor.md) — the optimization floor this profile should drive
- [`mlx.md`](mlx.md) — lazy evaluation and where it materializes
- [`hardware.md`](hardware.md) — bandwidth by SKU, for the roofline
