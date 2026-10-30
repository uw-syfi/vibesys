# Profiling on CPU

No device timeline to correlate against — the host *is* the whole picture, which makes the altitude discipline in [`tooling/profiler.md`](../../tooling/profiler.md) simpler but not optional.

## Tools

| Altitude | Linux | macOS |
|:--|:--|:--|
| System / process | `perf record` / `perf stat` | Instruments Time Profiler, `sample` |
| Framework / op | torch profiler | torch profiler |
| Instruction-level | `perf annotate` | Instruments |

vibesys wires these as `ProfilerKind.LINUX_CPU` and `ProfilerKind.MACOS_CPU`.

## What to look at first

`perf stat` before `perf record`. The counters answer the structural question immediately:

| Counter pattern | Meaning |
|:--|:--|
| Low IPC, high cache misses | bandwidth/locality bound — the common decode case |
| High IPC, high cycles | genuinely compute-bound; check ISA width is being used |
| High context switches | thread oversubscription — see [`floor.md`](floor.md) §1 |
| Cross-socket memory traffic | NUMA placement problem |

## The characteristic CPU findings

**Thread oversubscription.** Concurrent requests each spawning an intra-op pool produces more runnable threads than cores; `perf stat` shows the context-switch count and time goes to the scheduler rather than the model.

**Narrow-ISA build.** A generic build leaves AVX-512/AMX/SVE unused. This does not appear as a hotspot — everything is uniformly slower — so it is invisible to `perf record` and only shows in absolute throughput against a roofline. Check the build configuration before profiling.

## What the contract's table maps to

Most accelerator rows are N/A here. The two that survive:

- "Memory-bandwidth bound at decode" — applies, and is the usual answer. Remedy is quantization ([`floor.md`](floor.md) §2).
- "High utilization but low throughput" — applies; 100% CPU can be spin-waiting or scheduler thrash.

## See also

- [`floor.md`](floor.md) — threading, quantization, ISA width
- [`hardware.md`](hardware.md) — what to check about the target CPU
