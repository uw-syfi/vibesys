# PyTorch for serving

PyTorch is the default serving framework for vLLM, SGLang, and TensorRT-LLM's PyTorch runtime. This skill covers the serving-specific idioms — not general PyTorch.

## Weight loading from HuggingFace

```python
from transformers import AutoConfig, AutoModelForCausalLM

config = AutoConfig.from_pretrained(model_id, trust_remote_code=trust)
model  = AutoModelForCausalLM.from_pretrained(
    model_id,
    torch_dtype=torch.bfloat16,
    device_map="cuda",
    token=hf_token,
)
```

Production engines usually skip `AutoModelForCausalLM` and instead:

1. Instantiate their own `nn.Module` (custom architecture).
2. Stream-load weights from safetensors files via `safetensors.safe_open`.
3. Apply a key-remapping dict: HF names → engine-internal names.
4. Do Q/K/V fusion and gate/up fusion at load time.

Pattern for fusion:

```python
# HF: q_proj.weight, k_proj.weight, v_proj.weight (each H × H)
# Engine: qkv_proj.weight ((3*H_q + 2*H_kv) * head_dim × H)
qkv_weight = torch.cat([q_weight, k_weight, v_weight], dim=0)
engine_model.layers[i].qkv_proj.weight.copy_(qkv_weight)
```

Skip `.to(device)` per-tensor; load directly to device with `device_map=` or explicit `.cuda()` once per tensor.

## `inference_mode` vs `no_grad`

Use `torch.inference_mode()` in serving hot paths — stricter than `no_grad`, skips version-counting on tensors, slightly faster for small ops.

```python
with torch.inference_mode():
    logits = model(input_ids)
```

## torch.compile for serving

Two compile modes relevant to serving:

| Mode | When to use |
|:-----|:------------|
| `mode="default"` | reasonable baseline; some kernel fusion |
| `mode="reduce-overhead"` | **uses CUDA graphs under the hood**; best for static-shape decode loops |
| `mode="max-autotune"` | aggressive; long compile time, production-ready |

### Dynamic shapes

Variable-length serving breaks naive compile. Mark dims dynamic:

```python
input_ids = torch.zeros(1, seq_len, dtype=torch.long, device="cuda")
torch._dynamo.mark_dynamic(input_ids, 1)  # seq_len dim is dynamic
model_compiled = torch.compile(model)
```

Without `mark_dynamic`, Dynamo specializes on every new sequence length, recompiling repeatedly.

### Graph breaks

Dynamo recompiles at every graph break (Python branch it can't trace, mutation it can't track, unsupported op). In serving this is expensive. Diagnose:

```bash
TORCH_LOGS="graph_breaks" python engine.py
```

Common causes:
- `if tensor.item() > 0:` (CPU sync)
- Python lists being mutated
- `.to("cpu")` inside traced region
- Custom ops without schema

Move these outside the compile region or register custom ops (below).

### Custom ops

Kernels written in CUDA / Triton must be registered for Dynamo to see them cleanly:

```python
@torch.library.custom_op("my_ns::my_kernel", mutates_args=())
def my_kernel(x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    return _triton_impl(x, y)

@my_kernel.register_fake
def _(x, y):
    return torch.empty_like(x)  # shape/dtype inference for Dynamo
```

The `register_fake` (meta) function lets Dynamo reason about output shape without executing the op.

For Triton specifically, `@torch.library.triton_op` and `@torch.library.wrap_triton` are the preferred wrappers.

## Distributed

### Process group setup

```python
import torch.distributed as dist
dist.init_process_group(backend="nccl", ...)
# rank, world_size, local_rank from env or dist APIs
torch.cuda.set_device(local_rank)
```

Serving engines layer their own TP/PP/EP groups on top of the world group:

```python
tp_group = dist.new_group(ranks=list(range(0, tp_size)))
ep_group = dist.new_group(...)
```

### NCCL tuning

| Env var | Effect |
|:--------|:-------|
| `NCCL_ALGO=Tree` / `Ring` | algorithm for all-reduce |
| `NCCL_P2P_DISABLE=1` | force through NVSwitch — sometimes helpful on weird topologies |
| `NCCL_DEBUG=INFO` | verbose startup |
| `CUDA_VISIBLE_DEVICES` | GPU selection per rank |

Engines generally set sensible defaults; don't override without a reason.

## HuggingFace transformers integration

When pulling a model from transformers without forking the code:

- **`config.attn_implementation`**: `"eager"`, `"sdpa"`, `"flash_attention_2"`, `"flash_attention_3"`. Most engines override this with their own backend.
- **`generation_config`**: stop tokens, default sampling. Engines read this but usually let user sampling params override.
- **`trust_remote_code=True`**: needed for many new models that ship `modeling_*.py` in their HF repo. Security-sensitive — know what you're loading.
- **`use_cache=True` / `past_key_values`**: works but usually replaced by engine's own KV cache in production paths.

## Reducing CPU overhead in the hot path

For the serving-system view of this problem (AsyncScheduler / MRV2 / SGLang overlap), see [`algorithms/async-scheduling/`](../algorithms/async-scheduling.md). This section covers the PyTorch-level primitives production engines compose into those systems.

### Know every sync point

Every one of these forces CPU to block on the GPU:

| Operation | Why it syncs |
|:----------|:-------------|
| `x.item()` / `x.tolist()` / `float(x)` / `bool(x)` / `int(x)` | needs the value on CPU |
| `x.cpu()` or `x.to("cpu")` **without** `non_blocking=True` | waits for copy |
| `print(x)` / `str(x)` where `x` is on GPU | implicit `.item()` |
| `if x:` where `x` is a 0-d GPU tensor | `bool(x)` sync |
| `x.numpy()` | D2H + decode |
| `torch.tensor([...], device="cuda")` from CPU Python list | H2D + scalar dispatch |
| `torch.cuda.synchronize()` | explicit device-wide barrier |
| Index-assign `cpu_tensor[i] = gpu_tensor.sum()` | implicit sync on RHS |
| `assert` on a tensor condition | sync |

The production pattern: **one `.tolist()` at the very end** of a step (after all per-request decisions are packed into one tensor) — or zero CPU round-trips by keeping sampled tokens on-device until the next step's `input_ids` are constructed from them.

### Async copies and the pinned-memory race

Async D2H / H2D copies queue on the GPU stream and return immediately:

```python
x_cpu = torch.zeros(N, pin_memory=True)
x_gpu = x_cpu.to("cuda", non_blocking=True)   # queues; returns
# DO NOT mutate x_cpu before x_gpu is actually consumed
```

The race: if the CPU writes to `x_cpu` before the DMA has actually run, the GPU reads a half-written buffer. Two valid fixes:

1. **Event-based synchronize before re-use** — record a CUDA event after the copy, wait before next CPU write. Safe; serializes CPU and GPU around the barrier.
2. **Fresh pinned copy per use** (the MRV2 pattern) — keep the persistent state *unpinned*, allocate a throwaway pinned snapshot per step:
   ```python
   self.persist = torch.zeros(N, pin_memory=False)   # CPU-only, mutable freely
   self.persist[idx] = new_value
   tmp = self.persist.pin_memory()                    # fresh pinned snapshot
   gpu = tmp.to("cuda", non_blocking=True)            # DMA only touches tmp
   # self.persist is free for the CPU to keep mutating
   ```
   Cost: one pinned allocation + CPU memcpy per step. Benefit: no coordination needed. See [`algorithms/async-scheduling/`](../algorithms/async-scheduling.md) (vLLM MRV2 § "eliminate the race").

Rule: **`pin_memory=True` persistent + `non_blocking=True` = you owe a barrier**. If you don't want to pay the barrier, snapshot.

### Multiple CUDA streams and events

The primitive under every overlap-scheduler design. Pattern:

```python
forward_stream = torch.cuda.Stream()
copy_stream = torch.cuda.Stream()

with torch.cuda.stream(forward_stream):
    logits = model(input_ids)
    sampled = sample(logits)

# Copy on a different stream, but after forward completes
copy_stream.wait_stream(forward_stream)
with torch.cuda.stream(copy_stream):
    sampled_cpu = sampled.to("cpu", non_blocking=True)
    copy_done = torch.cuda.Event()
    copy_done.record(copy_stream)

# CPU is free to prepare the next batch here
prepare_next_batch(...)

# Only block when we actually need the result
copy_done.synchronize()
use(sampled_cpu)
```

Primitives:

| Construct | Meaning |
|:----------|:--------|
| `torch.cuda.Stream()` | a new stream separate from the default |
| `with torch.cuda.stream(s):` | ops enqueue to `s` while inside |
| `s2.wait_stream(s1)` | every future op on `s2` waits for everything currently on `s1` |
| `torch.cuda.Event()` | a point marker |
| `event.record(stream)` | mark the end of all ops currently on `stream` |
| `stream.wait_event(event)` | future ops on `stream` wait for `event` |
| `event.synchronize()` | block CPU until event completes |
| `event.query()` | non-blocking "has it fired?" |

Rules:
- **Never `torch.cuda.synchronize()`** in the hot loop — use an event.
- **Mixing default-stream and side-stream ops without events** silently reorders. Always record/wait explicitly.
- **`wait_stream` is one-shot** — it captures the current state of the other stream, not future ops on it.

### Preallocate static buffers

Each `torch.zeros/empty/tensor(...)` in the hot loop is a CUDA-caching-allocator malloc. Do it once:

```python
# Init
self.input_ids_buf = torch.zeros(max_bs, dtype=torch.long, device="cuda")
self.pos_ids_buf   = torch.zeros(max_bs, dtype=torch.long, device="cuda")

# Hot loop (in place)
self.input_ids_buf[:bs].copy_(current_input_ids)
self.pos_ids_buf[:bs].copy_(current_pos_ids)
logits = model(self.input_ids_buf[:bs], self.pos_ids_buf[:bs])
```

Mandatory for CUDA-graph capture (addresses must be stable). Helps eager too by avoiding allocator pressure.

### Gather-based input prep — skip reorderings

Older engines kept active requests contiguous in persistent state, requiring a tensor-wide shuffle on every join/finish. Modern pattern (MRV2 persistent-batch-v2): **permanent row per request, gather for the step**.

```python
# Persistent: permanent slot per request for its lifetime
self.states = torch.zeros(max_reqs, state_dim, device="cuda")
# Active mask lives on the CPU
active_rows = torch.tensor([i for i in range(max_reqs) if self.is_active[i]],
                           device="cuda", dtype=torch.long)

# Per-step gather — one GPU kernel
batch_input = self.states.index_select(0, active_rows)
```

`index_select` is O(active) with one kernel; whole-tensor reordering is O(max_reqs) per join/finish event. For serving with churn, the gather approach wins easily.

### Batched ragged writes

Hundreds of small per-request mutations each step (`tensor[row, col:col+k] = values`) become hundreds of kernel launches. Buffer and flush once:

```python
class StagedWriteTensor:
    def __init__(self, size, dtype, device):
        self.tensor = torch.zeros(size, dtype=dtype, device=device)
        self._pending = []  # (row, start, values)

    def stage_write(self, row, start, values):
        self._pending.append((row, start, values))

    def apply_write(self):
        # pack diffs into compact index/value tensors
        # one D2H + one scatter kernel
        self._pending.clear()
```

Production implementation: `vllm/v1/worker/gpu/buffer_utils.py::StagedWriteTensor`. Applies to block tables, `num_computed_tokens`, LoRA routing indices — any "many small writes per step" pattern.

### Keep the op stream in C++ wherever possible

Every Python-side `if`, `for`, or dict lookup between ops is CPython overhead. Techniques, in order of increasing effort:

- **`torch.compile`** — turns a Python op sequence into a compiled graph.
- **Custom ops with `torch.library.custom_op`** — a multi-op sequence appears as one op to Dynamo.
- **Vectorize per-request Python loops** — especially logits processors: apply as one batched op, not one per request.
- **`torch.library.triton_op` / `wrap_triton`** — Triton kernels visible to the compile stack.

Measure: `nsys` timelines show visible gaps between kernels — those gaps are usually Python.

### Warmup discipline

Required before any CUDA-graph capture or Triton-autotune-backed kernel:

```python
# Cover every shape the engine will see
for bs in (1, 2, 4, 8, 16, 32):
    for seq in (16, 64, 256, 1024):
        with torch.inference_mode():
            model(dummy_inputs(bs, seq))
torch.cuda.synchronize()
```

Skip it and the first request pays JIT compile + autotune latency (seconds), CUDA-graph capture can include compile work (corrupt graph), and early benchmark numbers are meaningless.

### Hot-path checklist

| ✓ | Rule |
|:--|:-----|
| □ | No `.item()` / `.tolist()` / `bool(tensor)` in the per-step path |
| □ | `torch.cuda.synchronize()` only in profilers / tests |
| □ | Static preallocated tensors for every per-step input |
| □ | `inference_mode()` wraps the step |
| □ | Async copies use fresh pinned snapshots *or* event-based sync |
| □ | Multi-stream regions have explicit `wait_stream` / events |
| □ | Persistent state is per-request-permanent; step input is gathered |
| □ | Many small writes → buffered + single flush, not launch per write |
| □ | Warmup covers every shape the engine will see |
| □ | `torch.compile` graph breaks counted and bounded (`TORCH_LOGS=graph_breaks`) |

## Pitfalls

- **Dynamo graph breaks silently increasing recompile count.** Check `torch._dynamo.config.cache_size_limit` and `TORCH_LOGS`.
- **Using `model.eval()` but not `inference_mode`.** `eval()` disables dropout/batchnorm but still tracks gradients; use `inference_mode` too.
- **Tensors leaked through Python lists.** Python-side list of past_key_values keeps old KV alive across steps; use a proper cache manager.
- **`nn.Parameter` vs `register_buffer`.** Parameters get saved in `state_dict` and copied by DDP; buffers are persistent-but-not-trained. Use buffer for fixed lookup tables (rotary-embedding tables, positions).
- **`model.to(torch.bfloat16)` then `.to(device)`.** Order matters for memory; prefer constructing in target dtype on target device.
- **Custom op without fake-tensor impl.** Works in eager; breaks under compile. Always register `register_fake`.
- **`torch.compile` + non-CUDA-graph-safe kernel.** `reduce-overhead` captures; autotune kernels break capture. See [`backends/triton-kernels/`](../backends/triton-kernels.md).

## See also

- [`algorithms/async-scheduling/`](../algorithms/async-scheduling.md) — the system-level view: how production engines (SGLang overlap, vLLM AsyncScheduler + MRV2) compose these primitives
- [`backends/cuda-graph/`](../backends/cuda-graph.md) — works hand-in-glove with `torch.compile(mode="reduce-overhead")` and requires the preallocation discipline above
- [`backends/triton-kernels/`](../backends/triton-kernels.md) — `torch.library.triton_op` integration, autotune-warmup rule
- [`algorithms/batched-sampling/`](../algorithms/batched-sampling.md) — another sync source to eliminate on the critical path
- [`algorithms/parallelism/`](../algorithms/parallelism.md) — distributed setup
- [`tooling/profiler/`](../tooling/profiler.md) — the only way to verify your overlap actually overlapped
- [`frameworks/mlx/`](mlx.md) — when on Apple Silicon instead
