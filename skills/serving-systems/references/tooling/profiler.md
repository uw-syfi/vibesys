# Profilers for serving

Three distinct tools at three different altitudes. Pick the right one for the problem — the wrong-altitude tool either misses the issue or drowns you in irrelevant detail.

## Which one to use — decision tree

```
Is the problem known to be inside a specific kernel?
  ├─ YES  → ncu (Nsight Compute)
  └─ NO  ─► Is it a Python / op-dispatch / framework problem?
             ├─ YES (suspected torch.compile, graph breaks,
             │         op-level hotspots) → torch profiler
             └─ NO  → nsys (Nsight Systems) to classify, THEN pick
```

**Always start with nsys** for a new performance problem unless you already have strong evidence the bottleneck is either kernel-local (ncu) or Python-local (torch profiler). nsys classifies; the other two answer specific questions.

## Quick comparison

| Aspect | torch profiler | Nsight Systems (nsys) | Nsight Compute (ncu) |
|:-------|:---------------|:----------------------|:---------------------|
| Altitude | Python ops / framework | system-wide timeline | single kernel |
| Time resolution | ms–μs | μs | ns / SM-cycle |
| Measures | op wall time, FLOPs (optional), memory | CPU/GPU timeline, kernel launches, memcpy, NCCL, NVTX | occupancy, roofline, warp stalls, memory throughput, instruction mix |
| Overhead | low (~2–10%) | low (~1–5%) | **high** (~10–100× slower per profiled kernel) |
| Run model | in-process | wrap `python engine.py` | target specific kernels |
| Output | Chrome trace JSON, stack-aware | `.nsys-rep` (NVIDIA GUI), SQLite export | `.ncu-rep` (NVIDIA GUI), CSV |
| Best at | finding a slow op in Python-heavy code | bottleneck classification, CPU/GPU overlap | kernel-level optimization |
| Worst at | real GPU work (kernels are aggregate) | kernel-internal metrics | system-wide picture |

## Golden rules (apply to all three)

1. **Profile steady state, not startup.** Exclude import, CUDA-context init, JIT / compile warmup, first-iteration cold effects.
2. **Constrain capture aggressively** — `--delay` / `--duration`, `cudaProfilerStart/Stop`, NVTX-triggered capture. A 2-minute full trace is almost always unreadable.
3. **Annotate with NVTX** so the timeline is self-describing: one range per iteration / forward / backward / dataloader / eval.
4. **Diagnose before editing code.** Produce: bottleneck class + evidence + bounded change set + acceptance metric.
5. **Verify every recommendation** by re-running the same benchmark and comparing the same metrics.

## Tool 1: PyTorch Profiler (`torch.profiler`)

In-process, Python-aware, fastest path to "which op is slow on the CPU side" and "does torch.compile actually fuse my code".

### Basic usage

```python
import torch
from torch.profiler import profile, record_function, ProfilerActivity, schedule

with profile(
    activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
    schedule=schedule(wait=1, warmup=2, active=3, repeat=1),
    on_trace_ready=torch.profiler.tensorboard_trace_handler("./trace"),
    record_shapes=True,
    with_stack=True,
) as prof:
    for step in range(6):
        with record_function(f"step_{step}"):
            run_one_step()
        prof.step()
```

- `wait` / `warmup` / `active` / `repeat` — the schedule object skips startup steps automatically.
- `record_shapes=True` — useful for correlating slow ops to tensor shapes.
- `with_stack=True` — attach Python stacks to each op (slower to profile, much more useful for Python-heavy bottlenecks).
- Output opens in Chrome `chrome://tracing`, Perfetto (recommended), or TensorBoard.

### `torch.profiler` for "my torch.compile doesn't help"

Dynamo graph breaks and recompiles show up clearly:

```bash
TORCH_LOGS="graph_breaks,recompiles" python engine.py
```

Graph breaks mean the captured graphs are small → little fusion. Pair with `torch._dynamo.config.cache_size_limit` tracking.

### When torch profiler is the right tool

- Suspected op-level hotspot (`model.to(device)` on hot path, Python loop inside decode, etc.).
- Investigating `torch.compile` fusion or `torch.compile(mode="reduce-overhead")` behavior.
- Attributing kernel time back to a Python stack frame.
- Rapid iteration (no external tools, just pip).

### When torch profiler is the wrong tool

- CPU-GPU overlap / idle-gap analysis — **use nsys**.
- Kernel-internal metrics (occupancy, warp stalls, roofline) — **use ncu**.
- Understanding NCCL collective patterns, multi-process / multi-GPU — **use nsys**.

## Tool 2: Nsight Systems (`nsys`)

The system-level timeline tool. One trace shows CPU threads, GPU activity, kernel launches, memcpy, NCCL / cuDNN / cuBLAS calls, and NVTX / PyTorch annotations in a unified view. **This is the first tool to reach for 80% of the time.**

### Commands

Minimal profile:
```bash
nsys profile -o report ./program
```

Useful serving tracing set:
```bash
nsys profile \
  --trace=cuda,nvtx,osrt,cudnn,cublas \
  -o report \
  ./program
```

Bounded capture (skip startup, profile 5 s window 10 s in):
```bash
nsys profile \
  --trace=cuda,nvtx,osrt \
  --delay=10 \
  --duration=5 \
  -o report \
  ./program
```

Export machine-readable data:
```bash
nsys stats report.nsys-rep
nsys export --type sqlite --output report_sqlite report.nsys-rep
```

PyTorch-oriented (autograd NVTX + process tree):
```bash
nsys profile \
  --trace=cuda,nvtx,osrt,cudnn,cublas \
  --sample=process-tree \
  --cpuctxsw=true \
  --pytorch=functions-trace,autograd-nvtx \
  -o report \
  python train.py
```

### Timeline patterns to recognize

| Pattern | Likely cause | Next step |
|:--------|:-------------|:----------|
| Long gaps between kernels | Python overhead, graph breaks, launch-bound | [`algorithms/async-scheduling/`](../algorithms/async-scheduling.md), [`backends/cuda-graph/`](../backends/cuda-graph.md) |
| H2D copies blocking the step | unpinned host memory, sync transfers | pin memory, `num_workers`, keep tensors on device |
| Frequent sync points | `torch.cuda.synchronize()`, `.item()`, blocking logging | remove syncs; batch scalar extraction |
| GPU busy, tight kernels, no gaps | kernel-bound | escalate to ncu |
| NCCL call dominates | TP collective cost | check NVLink topology; [`algorithms/parallelism/`](../algorithms/parallelism.md) |
| memcpy bursts before each step | dataloader pipeline | pinned mem + prefetch |

### When nsys is the right tool

- First diagnosis of a performance regression or slowness complaint.
- Suspected CPU-GPU overlap / async-scheduling / launch-overhead issue.
- Multi-GPU / NCCL / TP / EP communication cost analysis.
- Verifying that async scheduling / CUDA-graph capture / torch.compile actually improved the timeline.

### When nsys is the wrong tool

- Deciding *why* a specific kernel is slow internally — **use ncu**.
- Trivial single-op debugging — torch profiler is faster to spin up.

## Tool 3: Nsight Compute (`ncu`)

Kernel-level profiler. Measures SM-cycle-level metrics: occupancy, warp stalls, memory throughput, L1 / L2 cache behavior, roofline position, instruction mix. **High overhead** — only use on a targeted subset of kernels, after nsys confirms the workload is kernel-bound.

### Commands

Profile the default kernel set (small but serves):
```bash
ncu -o profile ./program
```

Deep dive on a specific kernel:
```bash
ncu --kernel-name myKernel --set full -o profile ./program
```

Roofline analysis (where the kernel sits on the arithmetic-intensity × peak-bandwidth-or-compute plane):
```bash
ncu --set roofline -o roofline ./program
```

Memory workload analysis (L1/L2 hit rates, coalescing, sector efficiency):
```bash
ncu --section MemoryWorkloadAnalysis -o memory ./program
```

Occupancy analysis:
```bash
ncu --section Occupancy -o occupancy ./program
```

Limit overhead:
```bash
# Only profile 3 invocations of the kernel, skip the rest
ncu --kernel-name myKernel --launch-count 3 -o profile ./program
```

### Interpretation quick-reference

| Metric | Meaning | What to do |
|:-------|:--------|:-----------|
| Low occupancy | too few warps resident per SM | reduce register pressure, shrink shared-mem usage |
| Memory-bound (low arithmetic intensity) | bandwidth-limited | try lower precision, fuse ops, check coalescing |
| Compute-bound with low utilization | good but unfused math | look for tensor-core opportunities |
| High sectors/request (> 4) | uncoalesced memory access | restructure strided access |
| Warp stalls: "Long Scoreboard" | waiting on global memory | prefetch, reorder, increase ILP |
| Warp stalls: "Barrier" | sync contention | reduce `__syncthreads()`, loop splits |
| Warp stalls: "No Instruction" | instruction fetch / IL | compiler issue; rare in practice |

### When ncu is the right tool

- A specific hot kernel identified by nsys.
- Writing or tuning a custom Triton / CUTLASS kernel.
- Quantifying an optimization's effect at kernel level.

### When ncu is the wrong tool

- Broad diagnosis (use nsys).
- Python / framework overhead (use torch profiler).
- Anything over ~5 hot kernels — the overhead and analysis time don't scale.

## Bottleneck taxonomy (classifier output)

After profiling, every report should end with **one primary bottleneck class** and optional secondary ones:

- `cpu_launch_bound` — CPU launches kernels slower than GPU consumes them.
- `python_overhead_bound` — Python dispatch / framework layers dominate.
- `input_pipeline_bound` — dataloader / preprocess stalls the GPU.
- `sync_bound` — `.item()` / `synchronize()` / blocking logging.
- `memcpy_bound` — H2D / D2H transfers dominate.
- `comm_bound` — NCCL / RDMA collective overhead.
- `kernel_bound` — GPU fully busy, kernel efficiency is the ceiling.
- `mixed_or_unclear` — genuinely ambiguous; often means the workload is well-balanced.

Each diagnosis should include:
1. evidence from the trace;
2. the suspected cause;
3. the top 1–3 edits likely to help;
4. the metrics that will confirm success.

## Workflow ladders

### Ladder A: initial triage of a slowness complaint

1. Reproduce with a fixed command.
2. `nsys` on a steady-state region.
3. Classify bottleneck.
4. If `cpu_launch_bound` / `python_overhead_bound` / `sync_bound` → [`algorithms/async-scheduling/`](../algorithms/async-scheduling.md), [`backends/cuda-graph/`](../backends/cuda-graph.md), [`frameworks/pytorch/`](../frameworks/pytorch.md).
5. If `input_pipeline_bound` / `memcpy_bound` → pinned memory, prefetch, overlap copies.
6. If `comm_bound` → [`algorithms/parallelism/`](../algorithms/parallelism.md), [`hardware/nvidia/`](../hardware/nvidia.md) topology.
7. If `kernel_bound` → escalate to ncu workflow (Ladder C).

### Ladder B: agent-guided fix loop

1. Baseline benchmark + profile.
2. Export stats / SQLite.
3. Summarize findings in the structured-feedback format below.
4. Let the coding agent patch.
5. Re-run same benchmark + profile.
6. Accept only if agreed metrics improve.

### Ladder C: kernel deep dive (only after nsys says kernel-bound)

1. Identify hot kernels (from nsys kern_sum or the timeline).
2. `ncu --kernel-name X --set full`.
3. Inspect occupancy / memory / warp stalls / roofline.
4. Recommend kernel or launch-config changes (or switch to a better backend — see [`backends/*`](../../backends/)).
5. Validate with reprofile.

## Agent-feedback format (for passing findings to a coding agent)

```
Goal:
Reduce steady-state median step time by 15% without changing numerics.

Profiler evidence:
- Capture excludes startup and warmup.
- Representative region: iterations 50–100.
- Tool: nsys (trace=cuda,nvtx,osrt).
- Long host gaps between kernels; GPU starvation during training_step.
- Frequent .item() on sampled token tensor.
- H2D copy bursts before each iteration.

Primary bottleneck:
python_overhead_bound

Secondary bottlenecks:
input_pipeline_bound

Allowed changes:
- Dataloader tuning (num_workers, pin_memory).
- Remove unnecessary .item() / synchronize calls.
- torch.compile / CUDA-graph-friendly refactors.

Disallowed changes:
- Model architecture / batch size / precision.

Acceptance criteria:
- Same benchmark command, inputs, capture window.
- Compare median step time, total idle-gap time, memcpy duration.
```

## Metrics to track across runs

Prefer machine-readable exports (`nsys stats`, SQLite) over screenshots:

- median and p95 step time
- total GPU idle-gap time
- number of kernel launches per step
- H2D / D2H duration + count
- sync API count / duration
- dataloader wait ranges
- hot kernel duration share
- NCCL / communication time (distributed)

## Anti-patterns

- Profiling startup and calling it representative.
- Comparing different input shapes across runs.
- Comparing a compiled run to an eager run without separating cold-start from steady-state.
- Overly broad traces that are impossible to interpret.
- Using ncu on every kernel before a systems-level diagnosis.
- "Increase batch size" without bottleneck evidence.
- Optimizing from screenshots instead of exported metrics.
- Assuming high GPU utilization means high kernel efficiency.

## See also

- [`tooling/serving-benchmark/`](serving-benchmark.md) — benchmark is the *input* to the profiler; get benchmark hygiene right first
- [`algorithms/async-scheduling/`](../algorithms/async-scheduling.md) — common fix when nsys says `cpu_launch_bound` / `python_overhead_bound`
- [`backends/cuda-graph/`](../backends/cuda-graph.md) — common fix when nsys shows gaps between kernels
- [`frameworks/pytorch/`](../frameworks/pytorch.md) — sync-point catalogue, multi-stream patterns, warmup discipline
- `agent-gpu-skills` `cuda-skill` — detailed ncu metric reference for kernel deep dives
