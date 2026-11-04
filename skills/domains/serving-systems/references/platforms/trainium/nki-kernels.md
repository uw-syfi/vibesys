# NKI kernels from native PyTorch

How to call Neuron Kernel Interface (NKI) kernels from a TorchNeuron serving
path, and which ready-made kernels ship in the container. For writing kernels,
use the vendored `neuron-nki-writing`, `-docs`, `-debugging`, `-profiling` and
`-profile-querying` skills; this file covers only the PyTorch integration.

Prerequisites: [`neuron-pytorch.md`](neuron-pytorch.md).

## When

Try `torch.compile` first. Reach for NKI when a profile shows one operator (or a
short sequence) dominating step time and the compiler's lowering is the
problem, or when PyTorch cannot express the operation efficiently (recurrent
state updates, fused quantized matmuls, packed layouts).

## Calling a kernel

```python
import nki, nki.isa as nisa, nki.language as nl
import torch
from torch_neuronx import nki_op

@nki.jit
def add_kernel(a, b):
    a_s = nl.ndarray(a.shape, dtype=a.dtype, buffer=nl.sbuf); nisa.dma_copy(dst=a_s, src=a)
    b_s = nl.ndarray(b.shape, dtype=b.dtype, buffer=nl.sbuf); nisa.dma_copy(dst=b_s, src=b)
    c_s = nl.ndarray(a.shape, dtype=a.dtype, buffer=nl.sbuf)
    nisa.tensor_tensor(dst=c_s, data1=a_s, data2=b_s, op=nl.add)
    out = nl.ndarray(a.shape, dtype=a.dtype, buffer=nl.shared_hbm)   # outputs live in HBM
    nisa.dma_copy(dst=out, src=c_s)
    return out

a = torch.rand(128, 128, device="neuron"); b = torch.rand(128, 128, device="neuron")
c = add_kernel(a, b)                       # direct call on neuron tensors: eager or in a compiled region

@nki_op("serve::nki_add", mutates_args={})  # register as a torch op (schema from annotations)
def nki_add(a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
    return add_kernel(a, b)

@torch.compile(backend="neuron", fullgraph=True)
def f(a, b):
    return nki_add(a * 2, b)                # the kernel is one node in the compiled graph
```

- Tensor arguments arrive in HBM; allocate outputs with `buffer=nl.shared_hbm`.
- **Non-tensor arguments are compile-time constants.** Each new value compiles
  the kernel again; keep them fixed (pass varying values as tensors).
- **In-place kernels:** list the written inputs in `mutates_args={"cache"}` and
  return `None`. This is how a kernel updates a resident KV cache or recurrent
  state.
- The kernel uses the process's logical-core (LNC) setting; do not index the
  kernel with an LNC (`kernel[lnc]`).
- SBUF/PSUM tensors need at least two dimensions; pass `[1, n]`, not `[n]`.
- Validate each kernel against a PyTorch reference in FP32 on CPU, in eager
  mode and inside the compiled region, before measuring speed.
- Keep a flag that turns each kernel off, so numerics and speed can be compared
  against the compiler's version at any time.

## Ready-made kernels: `nkilib`

The container ships AWS's NKI library (`nkilib`). Kernels are `@nki.jit`
functions; a sibling `*_torch.py` file is the PyTorch reference implementation.

| Family | Module | Notes |
|:--|:--|:--|
| Attention | `nkilib.core.attention` (`attention_cte`, `attention_tkg`, `attention_tkg_seq_packed`) | Prefill and decode attention with GQA |
| MLP | `nkilib.core.mlp` | Fused gate/up/down |
| QKV / output projection | `nkilib.core.qkv`, `nkilib.core.output_projection` | Fused projections |
| RMSNorm | `nkilib.core.rmsnorm` | Norm (+ quant variants) |
| Embeddings, top-k, router, MoE | `nkilib.core.embeddings`, `.topk`, `.router_topk`, `.moe`, `.moe_block` | |
| Gated DeltaNet | `nkilib.experimental.gdn` (`gdn_cte` prefill, `gdn_block_tkg` fused decode, `gdn_conv1d_*`) | Hybrid linear-attention layers (Qwen3.5-style) |
| KDA, MLA, sparse attention | `nkilib.experimental.attention`, `.mla`, `.sparse_attention_indexer` | |
| MXFP8 | `nkilib.experimental.*_mxfp8` | Trainium3 only |

`experimental` kernels change between releases; pin behavior with a reference
check. Read a kernel's docstring and its `*_torch.py` reference before wiring it
in: layouts (transposed weights, `[H, out_features]` kernel layout, tiled
inputs) are the usual integration bug.

## Pitfalls

- **Stale kernel after an edit outside the kernel's source.** The NKI trace
  cache keys on kernel source and arguments. Pass behavior-changing values as
  arguments, or run with `NKI_ENABLE_TRACE_CACHE=0`.
- **`ModuleNotFoundError: torch_neuronx.pyhlo`**: the kernel was called on
  `meta` or CPU tensors. Move inputs to `neuron`, or give `nki_op` a `fake_fn`.
- **Vendored skill snippets that use `torch_xla`**: replace
  `xm.xla_device()` with `torch.device("neuron")`; the kernel code itself is
  unchanged.

## See also

- [`attention.md`](attention.md): when to use the attention kernels.
- [`profiler.md`](profiler.md): finding the operator worth a kernel.
- AWS docs: [NKI kernels from torch-neuronx](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/torch/guides/how-to-work-with-nki.html).
