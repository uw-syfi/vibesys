# NxD Inference (NeuronX Distributed Inference)

Short pointer, by design. **NxD Inference** is AWS's turnkey inference library
in the Neuron SDK: you give it a supported model + a config and it handles
distribution, compilation, and serving on Trainium/Inferentia.

## What it gives you

- Pre-built modeling for popular architectures (Llama, Mixtral, …) with
  **minimal config**.
- Tensor + sequence **parallelism** and **weight sharding** across NeuronCores.
- **Continuous batching**, **KV cache**, flash-attention, **on-device sampling**,
  quantization — all wired up.
- Core API surface: `NeuronConfig`, `ModelBuilder`, `generate`.

It is built on `torch-neuronx` (it compiles via `neuronx-cc` like everything
else — see [`neuron-pytorch.md`](neuron-pytorch.md)).

## When to use it

- **Full turnkey** — "serve model X on Trainium" with the least code: let NxD own
  the whole model.
- **Building blocks in a bespoke server (recommended on Trainium)** — even when
  you write your own model layers, lean on NxD's **infrastructure**: its
  **`KVCacheManager`** for a **device-resident, in-place KV cache** and its
  **`ModelBuilder`** for tracing + the input/output aliasing that keeps that
  cache resident. This is the supported way to beat the host-bound decode trap;
  a from-scratch KV cache on raw `torch_neuronx.trace` cannot stay device-
  resident. **Start at [`nxd-kv-cache.md`](nxd-kv-cache.md).**

So you don't have to choose all-or-nothing: keep the architecture yours, borrow
NxD's KV-cache / tracing plumbing.

For the lower-level static-shape/compile-cache/BF16 mechanics read
[`neuron-pytorch.md`](neuron-pytorch.md); for custom kernels, the `neuron-nki-*`
skills.

Source: [NxD Inference docs](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/libraries/nxd-inference/index.html).
