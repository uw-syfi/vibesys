# PyTorch on AWS Trainium (native TorchNeuron)

How a PyTorch model runs on a NeuronCore with **TorchNeuron**, the native
PyTorch backend (`torch-neuronx` 2.13/2.14, Neuron SDK 2.33+). Read this before
writing a Trainium serving path. For hardware, see [`hardware.md`](hardware.md);
for the resident KV cache, [`static-kv-cache.md`](static-kv-cache.md); for custom
kernels, [`nki-kernels.md`](nki-kernels.md) and the vendored `neuron-nki-*` skills.

> Scope: native TorchNeuron only. The container has **no** `torch_xla`,
> `torch_neuronx.trace`, `neuronx_distributed` or NxD Inference. Older material
> (docs, the vendored NKI skills' harness snippets, forum answers) that uses
> `xm.xla_device()`, `torch_neuronx.trace`, `bucket_model_trace`,
> `NEURON_COMPILE_CACHE_URL`, `KVCacheManager` or `ModelBuilder` describes the
> retired XLA stack: do not port it.

## The mental model

Trainium is a standard PyTorch device named `neuron` (a `PrivateUse1` backend
that autoloads on `import torch`). Code written for CUDA ports by changing the
device string:

```python
import torch
import torch_neuronx            # only needed for its own APIs (synchronize, nki_op, metrics)

dev = torch.device("neuron")    # rank-local logical core; "neuron:0" in standalone scripts
model = MyModel(cfg).to(dtype=torch.bfloat16, device=dev).eval()
x = torch.randint(0, cfg.vocab, (B, L), device=dev)
```

Two execution modes, freely mixed on the same `neuron` tensors:

| Mode | How | Cost model |
|:--|:--|:--|
| **Eager** | Plain calls on `neuron` tensors | Each op compiles to its own NEFF on first use of a shape, then is cached. Fine for setup, correctness and prototyping; slow for a decode loop (one dispatch per op). |
| **`torch.compile`** | `torch.compile(fn, backend="neuron", fullgraph=True)` | Dynamo captures an FX graph, lowers it to StableHLO, `neuronx-cc` compiles **one NEFF per graph**. No TorchInductor. This is the serving hot path. |

Measured on a trn2 logical core, a toy decode step (QKV, KV write, SDPA, LM
head, argmax) ran **2.9 ms eager vs 0.22 ms compiled** with warm caches.
Compile the hot path.

`TORCH_NEURONX_ENABLE_CONCATENATION=1` (Adaptive Eager Execution) fuses queued
eager ops and narrows the gap for code you do not compile; it can shift
numerics slightly.

## torch.compile rules on Neuron

```python
step = torch.compile(decode_step, backend="neuron", fullgraph=True)
```

- **`fullgraph=True` always.** A graph break silently splits the region into
  several NEFFs; `fullgraph` turns it into an error you can fix.
- **`mode="default"` only.** `max-autotune`, CUDA graphs and
  `reduce-overhead` do not apply; other modes warn and fall back to default.
- **No dynamic shapes.** Leave `dynamic` unset or `False`; never
  `mark_dynamic`. Every new input shape or dtype is a new compile (seconds to
  minutes). Pad to a small ladder of **buckets** and pre-compile each at startup.
- **Mutating inputs is supported.** In-place writes to an input tensor
  (`cache.copy_(...)`, `index_copy_`) inside a compiled region update the
  caller's device buffer. That is how the KV cache stays resident; see
  [`static-kv-cache.md`](static-kv-cache.md).
- **Build lazy state before the first compiled call.** Dynamo guards on Python
  state the region reads; caches, masks or index tables built on first use
  cause recompiles. Compute them in eager mode, move them to `neuron`, pass them
  in.
- **No `.item()`, `print`, or data-dependent Python control flow inside a
  region.** Each forces a graph break and a host sync. Read scalars outside.
- **Forced breaks** (errors under `fullgraph`): `torch.sort`/`argsort` over a
  dimension longer than 16,384, `torch.topk` with `k >= 16384`,
  `torch._grouped_mm`, and any op with no Neuron lowering. `argmax` and small
  `topk` (sampling) are fine.
- **Fail-loud is the default.** `TORCH_NEURONX_DISABLE_FALLBACK_EXECUTION=1`
  makes a compile failure raise instead of silently running the region eagerly.
  Keep it.

Find breaks and recompiles before tuning anything:

```bash
TORCH_LOGS="graph_breaks,recompiles" python server.py
```

```python
from torch_neuronx.neuron_dynamo_backend import get_dynamo_metrics, reset_dynamo_metrics
reset_dynamo_metrics(); step(*warm_inputs)        # compiles
reset_dynamo_metrics(); step(*warm_inputs)        # same shapes
assert len(get_dynamo_metrics()) == 0             # nothing recompiled
```

## Is it really on the NeuronCore?

- In eager mode an op without a Neuron kernel **runs on CPU with a warning**.
  The model still works, slowly. Check what fell back:

  ```python
  torch_neuronx.clear_op_tracking()
  run_one_request()
  print(torch_neuronx.get_fallback_ops())   # e.g. ['aten::cumprod.out', ...]
  ```

  `aten::_to_copy` from your own `.cpu()` reads is expected; anything on the
  hot path is not. Move it into a compiled region (which fails loudly instead),
  rewrite it with supported ops, or replace it with an NKI kernel.
- Tensors on CPU passed to a compiled region are auto-copied every call; keep
  weights, caches and per-step inputs on `neuron`.

## Caches (already set for you)

| Cache | Variable | Holds |
|:--|:--|:--|
| NEFF | `TORCH_NEURONX_NEFF_CACHE_DIR` | Compiled graphs and eager ops, keyed by lowered HLO + compiler flags + compiler version + LNC |
| HLO | `TORCH_NEURONX_HLO_CACHE_DIR` | Lowered StableHLO |
| NKI | `NKI_TRACE_CACHE_URL` | Compiled NKI kernels |

VibeSys's trainium backend points all three at a host-mounted directory, so a
warm bucket is a cache hit across rounds and container restarts. Changing
`NEURON_CC_FLAGS` or the LNC config invalidates every entry. A "slow startup"
is a cold cache or a new shape, not the model.

The NKI cache keys on kernel source and arguments only. If a kernel's
behavior depends on a helper module or environment variable you edited, pass
the value as a kernel argument or set `NKI_ENABLE_TRACE_CACHE=0` for that run,
otherwise you get a stale kernel.

## Device, dtype, weights

- **BF16.** The serving baseline. `float64` runs as `float32`;
  `torch.autocast(device_type="neuron")` works.
- **FP8 / MXFP8 need Trainium3.** On trn2, TorchNeuron does not run FP8 matmuls:
  dequantize FP8 checkpoints (`weight.float() * scale` per block, then
  `.to(torch.bfloat16)`) at load time, or write the FP8 path yourself in NKI.
- **Weights:** load with `safetensors` on CPU, cast, then `.to("neuron")` once
  at startup. `transformers` is not preinstalled; `uv pip install` it if you use
  it for config, tokenizer or a reference.
- **Memory:** each logical core has ~24 GiB HBM
  (`torch.neuron.get_device_properties(0).total_memory`). Check use with
  `torch_neuronx.memory_allocated()` and `torch_neuronx.memory_summary()`.
  A model above ~20 GiB of weights must be split across cores; see
  [`tensor-parallel.md`](tensor-parallel.md).

## Timing

Neuron work is enqueued asynchronously. Synchronize before reading the clock or
you measure enqueue time:

```python
torch_neuronx.synchronize(); t0 = time.perf_counter()
for _ in range(n): step(...)
torch_neuronx.synchronize(); dt = (time.perf_counter() - t0) / n
```

`.cpu()`, `.item()`, `.tolist()` and `print(tensor)` are sync points too: one
per decode step (for the sampled tokens) is fine; one per op is not.

## Gotchas

- **The first call per shape compiles.** Warm every bucket at startup; a
  request must never be the first to reach a shape.
- **Per-request Python work between steps** (tokenizing, building masks on CPU,
  copying them over) adds up at sub-millisecond step times. Build per-step
  inputs on device.
- **A core belongs to one process.** A second process on a core already in use
  fails with `RuntimeError: Failed to initialize Neuron Runtime`; usually a stale
  server from an earlier attempt. Stop it first; pin disjoint core sets with
  `NEURON_RT_VISIBLE_CORES`.
- **Vendored NKI skill snippets** use `torch_xla`; call kernels on `neuron`
  tensors instead (see [`nki-kernels.md`](nki-kernels.md)).

## Related

- [`static-kv-cache.md`](static-kv-cache.md): resident, in-place KV cache
  under `torch.compile`.
- [`tensor-parallel.md`](tensor-parallel.md): sharding across logical cores.
- [`attention.md`](attention.md): SDPA, the built-in NKI flash path, NKI
  library attention kernels.
- [`continuous-batching.md`](continuous-batching.md): bucketed static-slot
  scheduling.
- [`pytorch.md`](../../frameworks/pytorch.md): portable PyTorch serving idioms.
- AWS docs: [TorchNeuron](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/torch/index.html),
  [torch.compile on Neuron](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/torch/compile/index.html),
  [environment variables](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/torch/reference/env-vars.html).
