# Consuming Triton kernels in serving

The gotchas of calling `@triton.jit` kernels inside a serving engine — different from tutorial-style use.

## Invocation basics

```python
import triton, triton.language as tl

@triton.jit
def my_kernel(X_ptr, Y_ptr, N, BLOCK_SIZE: tl.constexpr):
    pid = tl.program_id(0)
    ...

def call(x, y):
    N = x.numel()
    grid = (triton.cdiv(N, 256),)
    my_kernel[grid](x, y, N, BLOCK_SIZE=256)
```

Kernel arguments come in two flavors:

| Flavor | Declared | Purpose |
|:-------|:---------|:--------|
| Runtime | plain types | passed every call |
| Meta / compile-time | `: tl.constexpr` | part of the specialization key; triggers recompile when changed |

Every distinct combination of `constexpr` values produces a separate compiled kernel. Keep the `constexpr` set small or the cache blows up.

## Autotuning

```python
@triton.autotune(
    configs=[
        triton.Config({'BLOCK_SIZE': 128}, num_warps=4),
        triton.Config({'BLOCK_SIZE': 256}, num_warps=8),
    ],
    key=['N'],
)
@triton.jit
def my_kernel(...):
    ...
```

On first call for a given `key` tuple, Triton runs each config and caches the winner. Subsequent calls with a key-match skip autotune.

### Autotune pitfalls in serving

- **First-call latency spike.** Autotune runs N configs synchronously → dozens of kernel launches at the first forward. Warm up before serving production traffic.
- **Cache persistence.** By default the cache lives in memory per process. Set `TRITON_CACHE_DIR` to persist across restarts.
- **Too fine a `key`.** Keying on every input shape means a cold restart autotunes forever. Bucket shapes before passing as keys.

## Warmup pattern

Before opening the listen socket:

```python
for batch_size in (1, 2, 4, 8, 16, 32):
    for seq in (16, 64, 256, 1024):
        with torch.inference_mode():
            my_kernel_op(dummy_input(batch_size, seq))
torch.cuda.synchronize()
```

Cover the shapes you expect. Also captures all JIT compilation, which is in the launch path the first time.

## CUDA graph compatibility

Triton kernels are graph-safe **after** compile + autotune. The pitfalls:

| Issue | Fix |
|:------|:----|
| First call during capture triggers compile → corrupt graph | Warm up before capture |
| Autotune during capture launches multiple micro-kernels → replay diverges | Warm up before capture; or use non-autotuned configs in the capture region |
| Kernel uses `tl.atomic_*` on a pointer that may change | Ensure pointer addresses are stable across replays |
| Host-visible dispatch choices (Python `if`) | Pre-resolve before capture; inside the captured region you get one kernel variant |

In practice the "warm up before capture" rule solves most issues.

## torch.compile integration

Two patterns:

| Pattern | API |
|:--------|:----|
| Kernel opaque to Dynamo (pass-through) | `@torch.library.triton_op` + `@torch.library.wrap_triton` |
| Kernel fused by inductor | (none needed — inductor generates Triton itself) |

Using `torch.library.triton_op` lets Dynamo see the kernel as a black-box op with shape/dtype propagation — compatible with capture and `mark_dynamic`.

## Common kernel sources to reuse

| Source | What |
|:-------|:-----|
| [liger-kernel](https://github.com/linkedin/Liger-Kernel) | fused RMSNorm, RoPE, SwiGLU, cross-entropy, FlashAttention variants |
| [unsloth](https://github.com/unslothai/unsloth) | fused training + inference kernels |
| `sgl-kernel` (Triton ops under `python/sgl-kernel/`) | SGLang's Triton kernel library |
| `vllm/vllm/model_executor/layers/fused_moe/` triton kernels | MoE fused ops |
| `tensorrt_llm/triton_kernels/` | matmul_ogs.py, distributed.py, etc. |
| [flashinfer](https://github.com/flashinfer-ai/flashinfer) | most FlashInfer kernels are CUDA, but some utility ops are Triton |

## Debugging

```bash
# Inspect the generated PTX / SASS
TRITON_CACHE_DIR=./triton_cache python your_script.py
find triton_cache -name '*.ptx'

# Profile Triton launches specifically in nsys (they appear as regular CUDA kernels)
nsys profile --trace=cuda,nvtx python your_script.py
```

## Pitfalls

- **Autotune during graph capture** — leading cause of silent graph corruption.
- **Stale cache after kernel edit** — Triton's hash-based cache usually catches this, but if you edit imports or helpers it can miss. Blow away `TRITON_CACHE_DIR` when in doubt.
- **`num_warps` / `num_stages` mismatches across ops** — different configs mean different shared-memory budgets; co-scheduling two kernels may spill.
- **Incorrect masks at tile edges** — pure Triton author-side bug, but shows up when reusing a kernel at sizes it wasn't tested on.
- **Cross-architecture assumptions** — a kernel tuned for Hopper may be suboptimal or broken on Ampere / Blackwell. Gate on `torch.cuda.get_device_capability()`.

## Out of scope — kernel implementation

Writing Triton or Gluon kernels from scratch: see `agent-gpu-skills`'s `triton-skill`.

## See also

- [`backends/cuda-graph/`](cuda-graph.md) — capture-time pitfalls
- [`frameworks/pytorch/`](../frameworks/pytorch.md) — torch.compile integration
- [`tooling/profiler/`](../tooling/profiler.md) — analyzing launch behavior
