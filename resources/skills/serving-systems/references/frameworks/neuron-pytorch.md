# PyTorch on AWS Trainium (Neuron)

How a PyTorch model actually runs on a NeuronCore. Read this before writing a
Trainium serving path. For hardware, see
[`hardware/aws-trainium.md`](../hardware/aws-trainium.md); for custom kernels,
the vendored **`neuron-nki-*`** skills; for the generic (CUDA) PyTorch serving
idioms, [`pytorch.md`](pytorch.md).

> Scope: **Trainium2 + current Neuron SDK only.** Ignore `torch-neuron` (Inf1,
> archived) and legacy `transformers-neuronx` in older docs.

## The mental model

A NeuronCore has **no eager / op-by-op mode** like a GPU. Everything runs
through **XLA graph compilation**: the Neuron compiler (`neuronx-cc`) compiles a
graph **for a fixed set of input shapes** into a **NEFF** that the runtime
executes on-device. Your job is to get the hot path into a compiled graph and
keep its shapes stable. (You may see "TorchNeuron Native" eager / `torch.compile`
in the `/latest/` AWS docs — it is forward-looking and **not in the released SDK
/ inference DLC**, so don't plan around it. Even "eager-looking" `torch_xla`
code still traces and compiles a graph per shape.)

The one practical front-end is **`torch-neuronx` (XLA-based)**:

```python
import torch_neuronx
# Trace the forward at a FIXED shape → a compiled module you call per request.
compiled = torch_neuronx.trace(model, example_inputs)      # one shape
# or compile several buckets at once:
compiled = torch_neuronx.bucket_model_trace(model, bucketed_example_inputs)
```

`torch_neuronx.trace` / `bucket_model_trace` is the simplest path to a working
server: trace each shape bucket once, cache the NEFF, call the compiled module
per request. Tensors are placed on the XLA device under the hood (`torch_xla`);
use `xm.xla_device()` / the `torch_neuronx` placement helpers rather than
hand-driving `mark_step()` on the hot path.

## Static shapes are the whole game

Every distinct input shape = a separate `neuronx-cc` compile, and the **first
compile of a shape takes minutes**. A server that recompiles on the hot path is
unusable. So:

- **Bucket** prompt and decode lengths (e.g. prompt ∈ {128,256,512}, decode =
  one token against a fixed max KV length). Pad inputs up to the nearest bucket.
- **Pre-compile** every bucket at startup (warm the cache), not on the first
  request.
- Keep batch size fixed or bucketed too.
- A **static KV cache**: preallocate `[batch, max_len, kv_heads, head_dim]`
  HBM buffers and write at a position index — never grow tensors dynamically.

## Persistent compile cache

`neuronx-cc` caches NEFFs keyed by graph+shape. Point it at a directory that
survives restarts so you compile each bucket once, ever:

```bash
export NEURON_COMPILE_CACHE_URL=/opt/neuron-compile-cache   # dir or s3://...
```

VibeServe's trainium backend sets this and bind-mounts a host cache, so warm
buckets are instant across rounds. Treat a cache miss on the hot path as a bug.

## Device, dtype, weights

- **dtype: BF16.** The tensor engine runs BF16 at full rate; FP32 is ~4× slower.
  Do not run the hot path in FP32 or on CPU.
- **Device placement** is via the XLA device (`torch_xla`); for traced modules
  you pass tensors to the compiled callable (`torch_neuronx.move_trace_to_device`
  handles weights). There is no eager device to "just `.to('neuron')`" — the
  trace is the device path.
- **Weights**: load with `transformers` / `safetensors` on CPU, then let the
  trace/compile move them on-device. Build your own `nn.Module` with explicit
  layers (see [`pytorch.md`](pytorch.md) for fusion/remapping patterns) — the
  Neuron path is orthogonal to how you author the module.

## Gotchas

- **`.item()` / host syncs per token** serialize the decode loop — they force the
  device to finish and copy to host. Keep sampling on-device where possible;
  read back once per step at most.
- **Dynamic control flow** (data-dependent `if`/loops over tensor values) breaks
  graph capture or forces recompiles. Make the graph shape-static; do control
  flow in host Python *around* fixed-shape device calls.
- **Variable sequence length** → bucket + pad + attention mask, never a fresh
  shape per request.
- **Multiple logical cores**: tensor/sequence parallelism across the chip's
  cores is an optimization; an 8B model fits one core's reach via HBM. Reach for
  it only after single-core is clean.

## When to drop to NKI

If a specific op is the bottleneck and the compiler won't generate good code for
it (a fused attention/norm, an unusual layout), write it in **NKI** — use the
`neuron-nki-writing` / `neuron-nki-docs` skills. That's the Trainium analog of
dropping to a Triton/CUDA kernel.

## Related

- [`nxd-inference.md`](nxd-inference.md) — the high-level AWS inference library
  (what NOT to import if the task wants a from-scratch model, but useful for the
  patterns).
- [`algorithms/continuous-batching.md`](../algorithms/continuous-batching.md),
  [`algorithms/paged-attention.md`](../algorithms/paged-attention.md) — the
  serving algorithms still apply; just implement them over static-shape graphs.
