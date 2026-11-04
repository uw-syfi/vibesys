# Profiling on Trainium

`neuron-explorer` is the system-timeline and device-trace tool. It replaces `nsys` in the workflow described by [`tooling/profiler.md`](../../tooling/profiler.md) — read that first for the altitude discipline; this file covers only what is Neuron-specific.

## Capture

Profiling is driven by the Neuron runtime, not by wrapping the client. Two ways, both verified with native TorchNeuron on trn2:

**Whole process, no code change (servers).** Start the *server* process with the runtime's inspect variables, drive a short load, then stop it with SIGTERM; the runtime writes the profile on exit.

```bash
NEURON_RT_INSPECT_ENABLE=1 \
NEURON_RT_INSPECT_DEVICE_PROFILE=session \
NEURON_RT_INSPECT_OUTPUT_DIR=/tmp/neuronprof \
  <server command>
```

`session` captures all device activity per core into one NTFF. The default device mode (`1`/`model`) needs synchronous execution and produces **no** NTFF under TorchNeuron's asynchronous execution. Session traces grow fast (gigabytes per minute of load): keep the profiled window to seconds.

**Device profiling costs HBM.** Session device profiling reserves about 4.75 GiB per logical core:

| `NEURON_RT_INSPECT_DEVICE_PROFILE` | HBM allocatable per trn2 logical core (measured) |
|:--|:--|
| unset / `0` (system profile only) | 23.5 GiB |
| `session` | 18.75 GiB |

A server that needs more than about 18.7 GiB per core fails to load under device profiling (`NRT_RESOURCE`, `Failed to allocate resource`). Then capture in this order:

1. The production configuration with `NEURON_RT_INSPECT_DEVICE_PROFILE=0`: per-execution device time per NEFF, host gaps between executions, collectives. No HBM reserved.
2. Only for a kernel-level question, a session device profile of a smaller configuration that fits (fewer slots or a shorter context). Label it as not the production shape.

Drivers older than the SDK's requirement disable host-to-device DMA notifications; `neuron-explorer` then reports `invalid DMA duration` and DMA timings are unreliable.

**In process (a step loop).** `torch.profiler` with TorchNeuron's `NeuronConfig` adds PyTorch ops and the dispatch timeline:

```python
from torch.profiler import ProfilerActivity, profile
from torch_neuronx.profiling import NeuronConfig, NeuronProfiler, ProfileMode

cfg = NeuronConfig(modes=[ProfileMode.DEVICE, ProfileMode.RUNTIME], profile_output_dir="/tmp/neuronprof")
with profile(activities=[ProfilerActivity.CPU, ProfilerActivity.PrivateUse1],
             experimental_config=cfg, on_trace_ready=NeuronProfiler(cfg).export_trace):
    for _ in range(5): step(*inputs)
    torch_neuronx.synchronize()          # or the export races the queued work
```

This path writes NEFFs under `<out>/neffs/`, outside the session directory; pass one to `neuron-explorer view --neff-path` when the summary asks for it. `ProfileMode.DEVICE` reserves ~5 GB of HBM.

The invariant that matters most: **exclude compilation.** Warm every bucket first, run enough steps to reach steady state, then capture. A profile full of first-call executions describes warmup, not the model.

## What the timeline shows

| Signal | Meaning |
|:--|:--|
| Long gaps between executions on the device track | host-bound — the queue is starving; look for per-op eager dispatch, `.item()`/`.cpu()` per op, or Python work between steps |
| Many small NEFFs per step | the hot path is running eagerly (or split by graph breaks); compile it with `fullgraph=True` |
| New NEFFs appearing mid-run | a shape outside the bucket ladder reached the compiler; fix the ladder |
| Large host↔device copies every step | the KV cache or weights are crossing the boundary — see [`static-kv-cache.md`](static-kv-cache.md) |
| CPU-side `aten::` ops between device executions | eager CPU fallback — check `torch_neuronx.get_fallback_ops()` |
| High SBUF/PSUM pressure, low tensor-engine utilization in attention | activation peak; use SDPA's flash path or an `nkilib` kernel ([`attention.md`](attention.md)) |

## Neuron-specific classification

The generic finding→fix table in the contract mostly transfers, with two substitutions:

- "Gaps between kernels → graph capture" becomes **"gaps between executions → compile the region, then check bucketing and host work between steps."** There is no capture step to add.
- "One kernel dominates → kernel-internal tools" routes to the `neuron-nki-profiling` and `neuron-nki-profile-querying` skills (table meanings in `neuron-explorer-profile-schema`), and to [`nki-kernels.md`](nki-kernels.md) for the fix.

## The characteristic Trainium finding

A decode that looks uniformly slow, with no single dominant operator, is usually either an eager hot path (thousands of tiny executions per step) or a cache crossing the device boundary every token. Both present as host-bound rather than as a slow kernel, which is why the system timeline is the right altitude to catch them.

## See also

- [`floor.md`](floor.md) — what the profile should be pushing you toward
- [`neuron-pytorch.md`](neuron-pytorch.md) — host-sync pitfalls, fallback detection
- `neuron-nki-profiling`, `neuron-nki-profile-querying`, `neuron-explorer-profile-schema` — kernel altitude
