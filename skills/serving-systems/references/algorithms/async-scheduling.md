# Async / overlap scheduling

The problem this solves: at low-to-medium batch size on a modern GPU, the CPU side of a Python serving engine is slow enough that the GPU sits idle between decode steps. Two kinds of stalls show up:

1. **Kernel-launch-level** — addressed by CUDA graphs and fused kernels (see [`backends/cuda-graph/`](../backends/cuda-graph.md)).
2. **Scheduler-level** — the Python scheduler picks the next batch, builds block tables, assembles attention-plan inputs, handles detokenization + response push. At ~1–5 ms per iteration this dominates decode TPOT at batch 1–8.

Async scheduling fixes (2) by **pipelining CPU work with GPU compute**: while the GPU runs batch N, the CPU prepares batch N+1 and post-processes batch N−1.

## Three-stage pipeline — the mental model

```
time ──►
  GPU:  forward(N)          forward(N+1)        forward(N+2)
  CPU:  prepare(N+1)        prepare(N+2)        prepare(N+3)
        postproc(N-1)       postproc(N)         postproc(N+1)
```

Each "stage" is one engine step. Step N's GPU forward runs concurrently with step N+1's CPU prepare and step N−1's output postprocessing. When the pipeline is full, CPU time is hidden.

Requirements:

- **Stream ordering.** GPU forward, async D2H copies, and CPU waiters must be correctly ordered with CUDA events.
- **Future-typed data.** Step N+1's prep needs "what will step N sample?" This is a CUDA-side value not yet available. Solutions: index into a pre-allocated buffer (`FutureMap`, below) or return a `Future` from the executor.
- **Graceful serialization on edge cases.** Some states (structured-output grammar, spec-decoding verify, pipeline parallel) force one stage to block on another.

## SGLang: overlap scheduler (a.k.a. "zero-overhead")

Implementation lives in:

- `python/sglang/srt/managers/scheduler.py` — `init_overlap()`, `event_loop_overlap()`, `run_batch()` overlap path
- `python/sglang/srt/managers/overlap_utils.py` — `FutureMap`
- `python/sglang/srt/batch_overlap/operations.py` — `execute_overlapped_operations()`
- `python/sglang/srt/batch_overlap/two_batch_overlap.py` — `TboCudaGraphRunnerPlugin`, `TboForwardBatchPreparer`

### Streams

Two CUDA streams, explicitly coordinated:

| Stream | Work |
|:-------|:-----|
| `forward_stream` | model forward, sampler, `to("cpu", non_blocking=True)` D2H copy |
| `schedule_stream` | next-batch selection, block-table / KV-metadata tensor construction |

`self.forward_stream.wait_stream(self.schedule_stream)` orders the forward to start after the scheduler has finished producing the inputs it needs.

### CUDA events as the memory barrier

After D2H copy the scheduler records an event:

```python
# scheduler.py (run_batch, ~2811)
batch_result.copy_done = self.device_module.Event()
self.future_map.store_to_map(future_indices, batch_result)
batch_result.copy_to_cpu(return_logprob=batch.return_logprob)  # non-blocking
# (copy_done recorded inside copy_to_cpu)
```

The output processor blocks on this event only when it actually needs the result:

```python
# scheduler_output_processor_mixin.py (~392)
if result.copy_done is not None:
    result.copy_done.synchronize()
```

This defers the stall to as late as possible — typically the stall completes during the next step's GPU forward, so no wall-clock cost.

### FutureMap — the "future tokens" trick

The scheduler needs to know next-batch's input token IDs *before* the current batch has actually sampled them. Solution: pre-allocate indices into a circular buffer and use **negative indices as placeholders**.

```python
# scheduler.py (run_batch, ~2800)
future_indices = self.future_map.alloc_future_indices(bs)
batch_result = self.model_worker.forward_batch_generation(model_worker_batch)
self.future_map.store_to_map(future_indices, batch_result)
# Next batch constructs its input_ids using negative pointers:
future_indices_or_next_token_ids = -future_indices.indices
```

On the GPU, a tiny `resolve_future` kernel replaces negative indices with the actual sampled tokens from the buffer right before they are consumed — no CPU round-trip.

### CUDA-graph interaction — two-batch overlap (TBO)

Standard overlap uses eager execution. For CUDA graphs, SGLang uses **two-batch overlap**: a single graph captures two sub-batches with interleaved computation, so one half's attention runs while the other half's MLP runs. See `two_batch_overlap.py:TboCudaGraphRunnerPlugin` and `operations.py:execute_overlapped_operations`. TBO complements the scheduler-level overlap: graph replay stays uninterrupted, but compute units within the graph are kept busy.

### When overlap disables itself

From `is_disable_overlap_for_batch` (~scheduler.py:1468):

| Condition | Reason |
|:----------|:-------|
| Consecutive prefill batches (EXTEND→EXTEND) | improves TTFT; prefill is compute-bound so overlap serializes scheduling without helping |
| Spec V2 decode + active grammar | grammar needs current-step tokens to advance the FSM before the next batch can be prepared |
| Pipeline parallelism | PP needs synchronous output handoff; incompatible with deferred D2H |

## vLLM v1: two distinct pieces — AsyncScheduler and MRV2

vLLM v1 addresses CPU-overhead hiding at **two different layers**, with two separately-gated features that compose:

| Layer | V1 default | V2 / new |
|:------|:-----------|:---------|
| **Scheduler** (engine-level pipelining: batch N/N+1) | `Scheduler` | `AsyncScheduler` — gated by `scheduler_config.async_scheduling` |
| **Model runner** (GPU-side per-step execution: input prep, copies, sampling) | `vllm/v1/worker/gpu_model_runner.py::GPUModelRunner` | `vllm/v1/worker/gpu/model_runner.py::GPUModelRunner` — "**MRV2**", gated by `VLLM_USE_V2_MODEL_RUNNER=1` |

Both are complementary — they live at different layers and can run independently or together. Covering each in turn.

## vLLM v1 part 1: AsyncScheduler (engine-level)

Implementation:

- `vllm/v1/core/sched/async_scheduler.py` — `AsyncScheduler` subclass of base `Scheduler`
- `vllm/v1/worker/gpu/async_utils.py` — `AsyncOutput`
- `vllm/v1/engine/core.py` — engine loop, `step_with_batch_queue()` (~445)
- `vllm/config/scheduler.py` — `async_scheduling` config flag (~146), `get_scheduler_cls()` (~168)

### AsyncOutput — separate copy stream

```python
# async_utils.py (~12)
self.copy_event = torch.cuda.Event()
with stream(copy_stream, main_stream):
    copy_stream.wait_stream(main_stream)
    self.sampled_token_ids = async_copy_to_np(sampler_output.sampled_token_ids)
    # ...
    self.copy_event.record(copy_stream)

def get_output(self):
    self.copy_event.synchronize()
    return ModelRunnerOutput(...)
```

Two streams: `main_stream` (forward + sampler) and `output_copy_stream` (D2H sampled tokens, logprobs). `copy_stream.wait_stream(main_stream)` guarantees copy starts only after the sampler has committed logits; `copy_event.synchronize()` is the latest-possible CPU block.

### Future-based executor API

`execute_model()` returns a `Future` instead of the concrete output:

```python
# uniproc_executor.py
max_concurrent_batches = 2 if async_scheduling else 1
```

The engine core doesn't `await` each future immediately; it returns to the scheduler loop, which schedules batch N+1. When the next iteration arrives at `sample_tokens`, it finds prior futures resolved.

### Batch queue — deeper pipeline

`step_with_batch_queue()` (core.py:445–559) adds another level: multiple batch futures are queued up; while the GPU works on N, the scheduler prepares N+1 and adds it; when N's `AsyncOutput` becomes ready, the next batch is popped and awaited. Depth is capped at 2 by default (`max_concurrent_batches`).

### Conditions that force serialization

| Condition | vLLM behavior |
|:----------|:--------------|
| Structured-output grammar token pending | `pending_structured_output_tokens=True` defers sampling; pipeline collapses for that iteration |
| Speculative decoding verify rejection | re-sampling happens on main stream, serializes |
| PP > 1 without explicit override | async path disabled (`core.py:1075`: `if self.use_pp and not self.scheduler_config.async_scheduling`) |
| CPU backend | `CpuPlatform.supports_async_scheduling()` returns False |

## vLLM v1 part 2: MRV2 — Model Runner V2 (model-runner-level)

**MRV2 ≠ AsyncScheduler.** AsyncScheduler pipelines the engine step. MRV2 rewrites the model runner that each step executes — the code that prepares input tensors from persistent state, launches the forward pass, samples, and writes back. Design doc: [`docs/design/model_runner_v2.md`](https://docs.vllm.ai/en/latest/design/model_runner_v2/) (also in the vllm tree).

Status: **experimental, opt-in**, gated by `VLLM_USE_V2_MODEL_RUNNER=1` (`vllm/envs.py:245`). Not default; see `vllm/v1/worker/gpu/README.md`.

Implementation:

- `vllm/v1/worker/gpu/model_runner.py::GPUModelRunner` (~line 106) — same class name as V1 but in the `gpu/` subdirectory
- `vllm/v1/worker/gpu/input_batch.py::InputBatch` (~line 36) and `InputBuffers`
- `vllm/v1/worker/gpu/buffer_utils.py::StagedWriteTensor` (~line 101)
- `vllm/v1/worker/gpu/cudagraph_utils.py::ModelCudaGraphManager` (~line 263)
- `vllm/v1/worker/gpu_worker.py` (~line 271–304) — dispatch: reads `VLLM_USE_V2_MODEL_RUNNER` and swaps in the new runner

### Problem 1: the race on shared pinned buffers

V1's pattern for passing per-request state to the GPU each step was a single persistent pinned tensor:

```python
# V1 pattern (simplified from the design doc)
self.states[req_idx] = new_req.data       # CPU writes a row
states = self.states.to("cuda", non_blocking=True)  # queues async D2H on GPU stream
# Python returns immediately — but the GPU hasn't actually read yet
```

`non_blocking=True` queues the copy on the GPU stream and returns to Python right away. If the CPU then writes `self.states[req_idx]` again (e.g., preparing the next step) before the queued copy has actually executed, the GPU reads a half-written buffer. **Race.**

V1's mitigation was an **async barrier** — a CPU↔GPU sync fence surrounding the critical section. All CPU work had to stay inside the barrier, which:
- reduced the effective overlap with the GPU forward,
- created bug surface (forgetting to extend the barrier around new CPU work silently re-introduced races),
- complicated AsyncScheduler integration (the scheduler wants exactly the opposite — free CPU work during GPU forward).

### MRV2's solution: "eliminate the race"

Rather than barrier around the shared tensor, **allocate a fresh pinned copy per step**:

```python
# MRV2 pattern (from the design doc, §3)
self.states = torch.zeros(max_num_reqs, dtype=torch.int32,
                          device="cpu", pin_memory=False)
# ...
self.states[req_idx] = new_req.data
tmp_states = self.states.pin_memory()          # fresh pinned buffer
states = tmp_states.to("cuda", non_blocking=True)
```

`pin_memory()` returns a new pinned tensor whose contents are copied from `self.states` synchronously on the CPU (cheap). The async D2H then reads from `tmp_states`, which no CPU code ever touches again. `self.states` stays unpinned and is freely mutable from the CPU with zero coordination. The race is gone **without any sync primitive** — no barrier, no event.

Tradeoff: one extra pinned allocation + CPU memcpy per step. In practice negligible for LLM serving sizes, and the gain is that *all* CPU work can now run freely during the GPU forward.

The exact pattern has begun to propagate beyond the model runner — `vllm/v1/attention/backends/flashinfer.py:679` carries a comment:
> `Since we do not have explicit synchronization in ModelRunnerV2, we do not pin / reuse CPU buffers to avoid a race condition between step N async copies to / GPU and step N+1 buffer updates.`

### Problem 2: persistent-state juggling in V1

V1 used persistent state tensors *directly* as model / sampler inputs, which forced layout / ordering constraints — when requests joined or finished, full tensor-wide reorderings were needed to keep active requests contiguous. Plus a `CachedRequestState` backup tracked the same information redundantly.

### MRV2's solution: persistent batch v2 + gather

Each request gets a **permanent row** in `InputBatch` for its lifetime (`max_num_reqs=1024` pre-allocated). Preemption is treated as completion. Per-step model inputs are **gathered** from persistent state via a GPU-parallel gather — no reordering, no backup state, and the gather is GPU-cheap.

### Other MRV2 pieces (not all async-specific but reduce CPU pressure)

| Piece | File | Role |
|:------|:-----|:-----|
| **StagedWriteTensor** | `buffer_utils.py` | batched ragged writes to GPU-resident tensors: `stage_write(row, start, value)` buffers diffs; `apply_write()` packs + D2H + launches one kernel. Used for block tables, `num_computed_tokens`. Replaces many per-request small writes with one per step. |
| **GPU-native input prep** | Triton kernels | build `input_ids`, `positions`, `query_start_loc`, `seq_lens` on the GPU via Triton. Enables cases where the CPU can't yet know values (spec-decode verify). |
| **UVA** | — | GPU kernels directly access CPU-resident tensors like `prefill_token_ids` via unified virtual addressing. |
| **Triton-native Gumbel sampler** | `sample/sampler.py` | stateless in-kernel RNG, top-k-before-logprob to save memory, `idx_mapping` indirection for spec decoding. |
| **Explicit `ModelCudaGraphManager`** | `cudagraph_utils.py` | standard PyTorch graph-capture APIs; MRV2 can capture multiple draft-model forwards into one graph. |

### How MRV2 composes with AsyncScheduler

They target different layers and stack:

- **AsyncScheduler alone (V1 runner)**: pipelines at the engine-step level but the underlying runner still uses V1's async-barrier dance to avoid the race. Works, less clean.
- **MRV2 alone (default Scheduler)**: no engine-level pipelining, but individual step's CPU work is cleanly decoupled from GPU. Not typically useful on its own.
- **MRV2 + AsyncScheduler**: the intended target. Engine pipelines steps; the runner at each step has zero sync points and unpinned-copy discipline. The design doc calls this "Async-First" (§2).

### Current limitations

From the design doc and `README.md`:
- Experimental; not feature-complete.
- Some V1 features (certain attention backends, specific spec-decode paths) haven't been ported.
- Off by default.
- A HACK comment at `model_runner.py:426` notes "for now since the worker is shared between MRV1 and MRV2...".

## How the two designs differ

| Aspect | SGLang overlap | vLLM AsyncScheduler |
|:-------|:---------------|:--------------------|
| Placeholder mechanism for "next input" | `FutureMap` negative-index circular buffer, resolved by GPU kernel | `Future` returned from executor, resolved by `.synchronize()` on copy event |
| Primary sync primitive | `torch.cuda.Event` on D2H copy (per batch) | `torch.cuda.Event` on D2H copy (per batch, same pattern) |
| Pipeline depth | ≤ 2 batches in flight (one forward, one preparing) | 2 (controlled by `max_concurrent_batches`) |
| CUDA-graph compat | eager by default; two-batch-overlap (TBO) as the graph-compatible variant | orthogonal — graphs capture forward, async sampling always applies after |
| Disables on | prefill-prefill, spec+grammar, PP | structured-output pending, PP (without override), CPU backend |

Conceptually identical: hide CPU sampler / scheduler / postproc behind GPU forward, using CUDA events for ordering. Practically: SGLang's FutureMap trick lets the *next* batch's input_ids be constructed without reading from the GPU at all, while vLLM's Future-based approach defers the actual D2H sync to just before it's needed.

The table above compares the **scheduler layer**. vLLM's MRV2 lives one layer below (the model runner inside each step) and has no direct SGLang analog — SGLang's equivalent concerns are distributed across its managers and two-batch-overlap machinery rather than refactored into a single "runner v2" module.

## TRT-LLM: the C++ escape

TensorRT-LLM bypasses the Python scheduler problem in both runtimes:

- **C++ runtime**: `cpp/tensorrt_llm/batch_manager/trtGptModelInflightBatching.cpp` contains the scheduler loop in C++. Python-side overhead simply doesn't apply — there is no Python per-step loop.
- **PyTorch runtime (newer)**: `tensorrt_llm/_torch/pyexecutor/py_executor.py` runs a Python loop but keeps it tight, using pre-allocated static buffers and avoiding graph breaks.

For workloads where async scheduling isn't enough, moving the hot loop to C++ is the next step — this is what TRT-LLM does natively.

## Multi-step scheduling — an older, adjacent pattern

Before async scheduling became standard, both engines explored **multi-step scheduling**: run `N` decode steps per scheduler invocation to amortize the Python overhead. A single `schedule()` call allocates the next N steps' worth of KV pages, then the worker runs N CUDA-graph replays in a loop before returning control.

Status in 2025–2026:

- vLLM v1 **replaced** multi-step with AsyncScheduler — the async approach is strictly more general and doesn't require the N-step commit-then-validate awkwardness.
- SGLang's overlap scheduler similarly supersedes an older multi-step experiment.
- The technique is still useful in limited-flexibility deployments (e.g., when you really can't afford Python at all between steps) but has been largely subsumed.

Related pattern in vLLM v1: `CUDAGraphMode.FULL` + `async_scheduling=True` + batched sampling gets most of the multi-step benefit without its constraints.

## How it stacks with the other overhead-reduction axes

Async scheduling is one of several orthogonal axes:

| Axis | What it hides | Skill |
|:-----|:--------------|:------|
| Kernel launch | per-kernel CPU launch cost (~50µs × N kernels per step) | [`backends/cuda-graph/`](../backends/cuda-graph.md) |
| Python per-op overhead | framework dispatch, .to(), .item() syncs | [`frameworks/pytorch/`](../frameworks/pytorch.md) (torch.compile) |
| Sampler sync | `.item()` per request per step | [`algorithms/batched-sampling/`](batched-sampling.md) |
| Scheduler / Python loop | pick-next-batch, metadata tensors, postproc | **this skill** |
| Multi-step amortization | scheduler once per N GPU steps | largely subsumed by the above |

**They stack.** Full-graph CUDA graph + batched sampling on GPU + AsyncScheduler together get the engine to the point where per-token decode latency is essentially just the GPU forward pass.

## When to enable / disable

Enable async scheduling if:

- Workload is batch-1-to-16 interactive decode (CPU overhead shows up relative to small GPU work).
- Engine uses the supported executor (uniproc, multiproc; Ray has its own async path).
- No pipeline parallelism, no heavy structured-output at every step, no spec-decode-with-grammar.

Leave it off / expect degradation for:

- Pure throughput benchmarking at very large batch (CPU overhead is already amortized by batch size).
- Structured-output-heavy workloads where the grammar state frequently forces serialization.
- Workloads that need pipeline parallelism (or set the override flag carefully).

## Pitfalls

- **Reading sampled tokens too early.** Any CPU code that touches the sampled token tensor before the copy event has fired will silently block (if you call `.item()`) or read garbage (if you bypass sync). The engine must funnel all reads through the sync point.
- **Streams without events.** Adding a background stream without `wait_stream` / events introduces data races — the scheduler will sometimes read before the copy completes. Symptom: sporadic wrong tokens.
- **Graph capture inside an event-ordered region.** CUDA graph capture has restrictions on what streams may be active; overlap-style multi-stream code must be captured with care or fall back to eager on the graph's boundary.
- **Pipeline stall on grammar.** When structured output blocks sampling, the batch queue drains and TPOT spikes. Often OK in practice (grammar is usually a small fraction of steps) but worth measuring.
- **Debugging is harder.** Traces show events and streams interleaved; reproducing a bug requires fixing stream scheduling order. nsys timelines help — see [`tooling/profiler/`](../tooling/profiler.md).
- **Two-batch overlap (SGLang TBO) is not free on all shapes.** The split into two sub-batches requires even divisibility and certain layout assumptions; shapes that don't fit fall back to standard replay.
- **`max_concurrent_batches > 2` looks tempting.** It isn't — deeper pipelines add latency (requests wait in the queue) and memory (KV for in-flight batches). 2 is the sweet spot.

## See also

- [`algorithms/continuous-batching/`](continuous-batching.md) — the scheduling substrate that async scheduling overlays
- [`algorithms/batched-sampling/`](batched-sampling.md) — removes another sync source on the same critical path
- [`backends/cuda-graph/`](../backends/cuda-graph.md) — hides kernel-launch overhead; orthogonal to async scheduling
- [`engines/vllm/`](../engines/vllm.md), [`engines/sglang/`](../engines/sglang.md), [`engines/trtllm/`](../engines/trtllm.md) — production implementations
- [`tooling/profiler/`](../tooling/profiler.md) — required for debugging async-scheduling behavior on a timeline
- [`frameworks/pytorch/`](../frameworks/pytorch.md) — torch.compile / inference_mode as the per-op complement
