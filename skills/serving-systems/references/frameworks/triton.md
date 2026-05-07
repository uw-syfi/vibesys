# When to write a Triton kernel for serving

This skill is about the **decision** to write a Triton kernel — not the mechanics of writing one. Kernel-level authoring (block sizes, `tl.constexpr` tuning, warp specialization, Gluon on Hopper) lives in [`agent-gpu-skills`](https://github.com/slowlyC/agent-gpu-skills). This skill answers: does writing a custom kernel pay off here, or should I reuse what already exists?

## The default: don't write one

The fastest kernel is the one you don't write. Before considering a custom Triton kernel, check whether an existing library already covers the fusion:

| If you're thinking of... | Check first |
|:-------------------------|:------------|
| attention + anything | [`backends/flashinfer/`](../backends/flashinfer.md), [`backends/flashattention/`](../backends/flashattention.md) |
| RMSNorm, LayerNorm, SwiGLU, RoPE | flashinfer fused ops (`rmsnorm`, `fused_add_rmsnorm`, `silu_and_mul`, `apply_rope_pos_ids`); [liger-kernel](https://github.com/linkedin/Liger-Kernel) |
| cross-entropy + logits scale | liger-kernel, flashinfer `logits_processor` |
| MoE grouped-GEMM + dispatch | flashinfer `fused_moe`, `flashinfer.gemm.group_gemm_*`, vLLM fused_moe, SGLang `sgl-kernel` / CUTLASS-MoE |
| quantized GEMM (FP8 / INT4) | flashinfer.gemm, Marlin (NVIDIA), hipBLASLt (AMD) |
| paged KV append + RoPE | flashinfer (`append_paged_kv_cache` + `apply_rope_pos_ids_inplace`) |
| batched sampling | flashinfer.sampling + `LogitsPipe` |

If your fusion is on this list, reusing wins on perf *and* maintenance. A custom Triton kernel is worth considering only when the answer is genuinely "none of the above".

## When custom Triton does make sense

The pattern: **a sequence of ops in your hot path that no existing library fuses, where each op's intermediate tensor is large enough that kernel-launch + HBM round-trip is a measurable cost.**

Fusion opportunities that tend to pay off in serving:

| Pattern | Why it helps | Real examples |
|:--------|:-------------|:--------------|
| **Quant + GEMM epilogue** | avoid writing a full-precision intermediate then re-reading it | custom AWQ variants, model-specific scale layouts |
| **Norm + residual-add + norm** | one HBM round-trip for two norms + a residual | liger-kernel `rms_norm` does the pre-attn variant; the *post*-attn variant may still be yours |
| **Activation + quant + scatter** (MoE epilogue) | avoid materializing per-expert float outputs | similar to flashinfer's `silu_and_mul_scaled_nvfp4_experts_quantize` but your model may have different scale layout |
| **Logits processor stacks** with custom penalty | fuse temperature + custom-bias + mask + sample | flashinfer `LogitsPipe` covers standard; custom if you have a bespoke penalty |
| **Custom attention mask** (tree verify, sparse) | existing kernels don't know your mask | tree attention for speculative decoding |
| **Sample-the-next-step using prior KV** | fuse compute + gather for speculative-decode drafters | model-specific, unusual |
| **Custom position encoding** | M-RoPE, 3D RoPE for video models, custom scaling | some Qwen3-VL / video-gen paths |

Counter-pattern: anything that's basically "matmul with a twist" — unless it's MoE, the twist usually doesn't justify a Triton kernel vs. an epilogue in CUTLASS / cuBLASLt.

## Decision rubric

Before committing to a kernel, check all of:

1. **Is the op sequence actually hot?** Profile first ([`tooling/profiler/`](../tooling/profiler.md)). If the kernels you'd fuse are <5% of step time, skip.
2. **Are the intermediate tensors large?** Fusing two ops that each touch <1 MB buys ~tens of microseconds. Not worth it. Fusing two ops each touching tens of MB buys milliseconds.
3. **Is the shape landscape bounded?** Triton autotune multiplies compile cost by the cross-product of constexpr configs × shape keys. An op with 10 possible shapes × 5 autotune configs = 50 compiles on cold start.
4. **Do you control the model?** If weights / layout come from a third party and might change, your fused layout can break.
5. **Is the fusion on the CUDA-graph critical path?** If yes, the kernel must be warmup-safe (no autotune during capture).
6. **What's the maintenance cost?** One kernel to keep working across 2–3 hardware generations × PyTorch versions × precision variants is nontrivial.

If the answer to 1-2 is strongly yes and 3-6 are manageable, write it. Otherwise, don't.

## Integration with the engine

Writing the kernel is half the job; wiring it in without breaking `torch.compile` / Dynamo is the other half.

### Wrap with `torch.library.triton_op`

The recommended pattern since PyTorch 2.4:

```python
from torch.library import triton_op, wrap_triton

@triton_op("my_ns::fused_rmsnorm_swiglu", mutates_args=())
def fused_rmsnorm_swiglu(x: torch.Tensor, w: torch.Tensor,
                         gate: torch.Tensor, up: torch.Tensor) -> torch.Tensor:
    out = torch.empty_like(x)
    _fused_kernel[grid](x, w, gate, up, out, ...)
    return out

@fused_rmsnorm_swiglu.register_fake
def _(x, w, gate, up):
    return torch.empty_like(x)
```

`wrap_triton` marks the kernel launch so Dynamo traces cleanly through it; `register_fake` supplies the meta-tensor implementation Dynamo needs for shape propagation.

### Without `triton_op` (older pattern)

```python
@torch.library.custom_op("my_ns::fused_op", mutates_args=())
def fused_op(x: torch.Tensor) -> torch.Tensor:
    return _triton_impl(x)

@fused_op.register_fake
def _(x):
    return torch.empty_like(x)
```

Works, but Dynamo sees it as an opaque call — less room for surrounding fusion.

### Autotune + CUDA-graph compatibility

Most Triton kernels in serving need autotune:

```python
@triton.autotune(
    configs=[
        triton.Config({'BLOCK_SIZE': 128}, num_warps=4),
        triton.Config({'BLOCK_SIZE': 256}, num_warps=8),
    ],
    key=['N'],
)
@triton.jit
def _fused_kernel(...):
    ...
```

**Autotune must complete before CUDA-graph capture.** If capture runs first call, you're capturing the autotune-probe launches, not the winner. Warmup pattern:

```python
# Warmup: cover every (bs, seq, ...) combination the engine will see
for shape in expected_shapes:
    fused_op(dummy_input(shape))
torch.cuda.synchronize()
# Now safe to capture CUDA graphs
```

Persistent cache: `TRITON_CACHE_DIR=/path/to/cache` survives process restart.

### Graph-safety of the kernel itself

- Arguments must be tensors, ints, or `constexpr` scalars — not Python objects.
- No `tl.atomic_*` on pointers that change across replays.
- No host dispatch inside the captured region — resolve all config choices before capture.
- Re-entry through `wrap_triton` is graph-safe.

## Which Triton?

Three flavors relevant to serving:

| Flavor | Upstream | Use when |
|:-------|:---------|:---------|
| **Stock Triton** (`triton-lang/triton`) | `pip install triton` | default; PyTorch bundles it |
| **Triton-AMD** | same repo, CDNA backend | serving on AMD ROCm |
| **Gluon** | Meta's Triton-on-steroids, Hopper-focused | when you need TMA + WGMMA explicit control; kernel-writing skill lives in agent-gpu-skills |

For non-NVIDIA targets, check that the kernel compiles on your Triton backend before committing. MI300 via Triton-AMD has narrower op coverage than NVIDIA Triton.

## Catalog of well-tested Triton-kernel libraries (reuse before write)

| Library | What | Source |
|:--------|:-----|:-------|
| **liger-kernel** | fused RMSNorm, RoPE, SwiGLU, cross-entropy, FlashAttention variants — training-plus-inference focus | https://github.com/linkedin/Liger-Kernel |
| **unsloth** | fused training + inference kernels | https://github.com/unslothai/unsloth |
| **sgl-kernel Triton ops** | SGLang's in-tree Triton kernel library | `repos/sglang/sgl-kernel/` |
| **vLLM fused_moe Triton ops** | MoE routing / dispatch / expert GEMM | `repos/vllm/vllm/model_executor/layers/fused_moe/` |
| **TRT-LLM triton_kernels/** | matmul_ogs, distributed, etc. | `repos/TensorRT-LLM/triton_kernels/` |
| **flashinfer Triton ops** | some utility ops (most FI is CUDA) | inside flashinfer-python |

## When custom kernel stops making sense

Stop and reconsider if:

- A new library release covers your fusion (re-evaluate every major flashinfer / liger-kernel / FlashAttention release).
- Hardware generation changes (kernel tuned for Hopper may be suboptimal on Blackwell; port or delete).
- Model architecture shifts (shape space blows up).
- The kernel has been buggy twice in a row — sign your fusion is more fragile than the wins justify.

## Out of scope

- **How to write Triton kernels** (block sizes, autotune config design, Hopper-specific features like TMA / WGMMA / warp specialization, Gluon) → [`agent-gpu-skills`](https://github.com/slowlyC/agent-gpu-skills) `triton-skill`
- **Using pre-written Triton kernels** in an engine (invocation, warmup, cache management) → [`backends/triton-kernels/`](../backends/triton-kernels.md)

## See also

- [`backends/triton-kernels/`](../backends/triton-kernels.md) — consuming existing Triton kernels
- [`backends/flashinfer/`](../backends/flashinfer.md), [`backends/flashattention/`](../backends/flashattention.md) — check these first before writing anything
- [`backends/cuda-graph/`](../backends/cuda-graph.md) — warmup discipline for custom kernels in captured regions
- [`frameworks/pytorch/`](pytorch.md) — `torch.library.triton_op` / `wrap_triton` / `register_fake`
- [`tooling/profiler/`](../tooling/profiler.md) — required before deciding anything needs fusing
