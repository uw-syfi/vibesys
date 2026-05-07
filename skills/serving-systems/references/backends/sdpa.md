# PyTorch SDPA for serving

Use SDPA when you want a dependency-light attention backend that stays inside
PyTorch and can be captured with CUDA graphs when shapes and addresses are
static. It is a good baseline for single-request or prototype servers; for
high-throughput continuous batching, prefer FlashAttention / FlashInfer /
Triton paged attention.

## Prerequisites

- PyTorch 2.x with `torch.nn.functional.scaled_dot_product_attention`
- A model that already computes Q/K/V and owns KV-cache storage
- CUDA graphs if using the capture patterns below

## Core call

```python
import torch.nn.functional as F

out = F.scaled_dot_product_attention(
    q,                          # [bs, num_q_heads, q_len, head_dim]
    k,                          # [bs, num_kv_heads, kv_len, head_dim]
    v,                          # [bs, num_kv_heads, kv_len, head_dim]
    attn_mask=mask,             # optional, broadcastable to [bs, heads, q_len, kv_len]
    dropout_p=0.0,
    is_causal=False,
    scale=head_dim ** -0.5,
    enable_gqa=num_q_heads != num_kv_heads,
)
```

For decode, `q_len == 1`. For Llama-style GQA, set `enable_gqa=True` when
`num_q_heads > num_kv_heads`.

## Single-batch CUDA graph patterns

CUDA graph replay requires the same operation graph, tensor addresses, and
tensor shapes as capture. Batch size alone is not enough: decode attention
shape also depends on the visible KV length.

### Pattern A: one graph per visible KV length

Capture each `kv_len` you expect to replay:

```python
static_q = torch.empty((1, n_q_heads, 1, head_dim), device="cuda", dtype=dtype)
static_mask = torch.zeros((1, 1, 1, kv_len), device="cuda", dtype=dtype)

graph = torch.cuda.CUDAGraph()
with torch.cuda.graph(graph):
    static_out = F.scaled_dot_product_attention(
        static_q,
        k_cache[:, :, :kv_len, :],
        v_cache[:, :, :kv_len, :],
        attn_mask=static_mask,
        dropout_p=0.0,
        is_causal=False,
        scale=head_dim ** -0.5,
        enable_gqa=True,
    )
```

Replay:

```python
static_q.copy_(q_step)
graph.replay()
```

Use this when `max_decode_len` is modest or you can bucket lengths. It avoids
wasted attention FLOPs but needs many graphs if every token length is captured.
Do not fall back to `torch.compile` for uncovered lengths in a benchmark unless
you want to measure Inductor cold/lazy setup.

### Pattern B: one fixed-max graph

Capture one graph with the full KV capacity and mutate only mask values:

```python
static_q = torch.empty((1, n_q_heads, 1, head_dim), device="cuda", dtype=dtype)
static_mask = torch.empty((1, 1, 1, max_kv_len), device="cuda", dtype=dtype)

graph = torch.cuda.CUDAGraph()
with torch.cuda.graph(graph):
    static_out = F.scaled_dot_product_attention(
        static_q,
        k_cache,                # full [1, n_kv_heads, max_kv_len, head_dim]
        v_cache,
        attn_mask=static_mask,
        dropout_p=0.0,
        is_causal=False,
        scale=head_dim ** -0.5,
        enable_gqa=True,
    )

def replay(q_step, visible_len):
    static_q.copy_(q_step)
    static_mask[..., :visible_len].fill_(0)
    static_mask[..., visible_len:].fill_(float("-inf"))
    graph.replay()
    return static_out
```

Use this for single-batch latency when CPU overhead dominates and `max_kv_len`
is not too large. It is one graph and no shape dispatch, but every token attends
over `max_kv_len`, so early tokens waste work.

### Pattern C: fixed buckets

Capture fixed capacity buckets such as `128, 256, 512, 1024, 2048`. For a
visible length `L`, replay the smallest bucket `B >= L` and mask `[L:B]`.
This keeps graph count small while bounding wasted FLOPs.

## What not to do

This is not graph-stable:

```python
k = k_cache[:, :, :cur_len, :]
v = v_cache[:, :, :cur_len, :]
out = F.scaled_dot_product_attention(q, k, v)
```

The `cur_len` slice changes the SDPA input shape and often changes backend
launch parameters. Capture either exact lengths or fixed buckets/max length.

## Backend selection

PyTorch chooses the SDPA backend automatically. In serving benchmarks, record
which path is active; on NVIDIA this may be cuDNN SDPA, flash SDPA, memory
efficient attention, or math fallback depending on dtype, masks, GQA, and
shapes. Use:

```python
from torch.nn.attention import sdpa_kernel, SDPBackend

with sdpa_kernel(SDPBackend.CUDNN_ATTENTION):
    out = F.scaled_dot_product_attention(...)
```

Only force a backend after checking it supports your shape and mask. Letting
PyTorch choose is usually safest for correctness.

## Microbenchmark expectation

For single-batch decode on H100-class GPUs, attention-only latency can be very
small. In one local probe with `bs=1`, `n_q_heads=32`, `n_kv_heads=8`,
`head_dim=128`, `max_kv_len=1024`, fp16:

| Pattern | Graphs | Mean attention time |
|:--------|------:|--------------------:|
| SDPA exact length graphs | 1024 | ~0.024 ms/token |
| SDPA fixed-max graph | 1 | ~0.012 ms/token |
| FlashAttention kvcache eager | 0 | ~0.024 ms/token |

This is attention-only GPU time. Full TPOT can still be dominated by QKV/MLP
matmuls, grammar masking, sampler readback, or CPU scheduling.

## Pitfalls

1. **Assuming batch size is the graph key.** Decode graph shape includes `kv_len`; key by exact length, fixed bucket, or fixed max length.
2. **Measuring `torch.compile` fallback as graph replay.** Inductor may lazily load/generated kernels for new shapes, causing GPU-idle gaps.
3. **Mask mutation cost.** Updating a full `max_kv_len` mask on GPU every token is cheap for small max lengths, but becomes visible at long context.
4. **CPU token readback.** `.item()`, `int(tensor)`, and `.tolist()` synchronize the stream. SDPA graph speed will not matter if the loop reads every sampled token on CPU.
5. **GQA support.** Pass `enable_gqa=True` for grouped-query models; otherwise K/V heads will not match Q heads.
6. **Backend drift.** PyTorch may switch SDPA backend across versions or shapes. Record PyTorch/CUDA versions and backend controls in benchmark notes.

## When to use SDPA vs other backends

| Backend | Best fit |
|:--------|:---------|
| SDPA fixed-max / bucket graph | Single-batch or low-concurrency latency prototypes; simple dependency story |
| FlashAttention | Eager decode / varlen prefill with strong kernels and paged KV contract |
| FlashInfer | Serving-oriented paged KV wrappers, planning APIs, fused ops |
| Triton custom attention | When you need fixed-grid or custom masking behavior not exposed by library kernels |

## Out of scope — kernel implementation

For writing a fixed-grid attention kernel that skips inactive pages inside one
CUDA graph, use agent-gpu-skills's CUDA / Triton / CUTLASS skills.

## See also

- [`backends/cuda-graph/`](cuda-graph.md) — capture lifecycle and full vs piecewise graph patterns
- [`backends/flashattention/`](flashattention.md) — FlashAttention integration and paged KV contract
- [`backends/flashinfer/`](flashinfer.md) — FlashInfer paged KV wrappers and fused ops
- [`frameworks/pytorch/`](../frameworks/pytorch.md) — PyTorch serving idioms and `attn_implementation`
