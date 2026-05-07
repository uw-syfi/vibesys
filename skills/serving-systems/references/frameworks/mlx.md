# MLX for serving

MLX is Apple's array framework designed for Apple Silicon. Naive PyTorch ports to MLX tend to underperform because the mental model differs — unified memory changes transfer semantics, lazy evaluation changes where sync happens.

## Core model

### Lazy evaluation

```python
import mlx.core as mx

x = mx.array([1, 2, 3])
y = x + 1           # no computation yet
z = y * 2           # still no computation
mx.eval(z)          # NOW compute
# or:
print(z)            # force eval implicitly
```

Ops build a graph. `mx.eval(*outputs)` forces computation. Any access that needs a concrete value (print, numpy conversion, `.tolist()`) triggers eval.

**Implication**: the equivalent of PyTorch's `.item()` latency isn't at the op call — it's at the next eval. Benchmark with explicit `mx.eval` + `time.perf_counter`.

### Unified memory

```python
# No .to(device). MLX arrays live in unified memory.
x = mx.zeros((1024, 1024))  # accessible to CPU and GPU without copy
```

Implications:
- Don't pipeline device transfers; there aren't any.
- Memory budget is shared with everything else on the Mac — size models conservatively.
- CPU and GPU reading the same buffer can cause cache-coherence stalls; keep producer / consumer on the same side when possible.

### mx.compile

```python
@mx.compile
def forward(x, w1, w2):
    return mx.matmul(mx.nn.relu(mx.matmul(x, w1)), w2)
```

JIT-compiles the graph, fuses ops, caches by traced shape + dtype. Similar role to `torch.compile`. Works with shape-stable decode loops; dynamic shapes force recompile.

## mlx-lm: the reference serving path

`mlx-lm` is the canonical LLM serving library for MLX:

```python
from mlx_lm import load, generate
model, tokenizer = load("mlx-community/Llama-3.2-3B-Instruct-4bit")
response = generate(model, tokenizer, prompt="Hello", max_tokens=256)
```

Features:
- Streaming generation (`stream=True`)
- KV cache (custom, not paged)
- Top-p / top-k / temperature / repetition penalty
- Many pre-quantized models on the `mlx-community` HF org
- Server mode: `mlx_lm.server` exposes an OpenAI-compatible API

Source: [`ml-explore/mlx-lm`](https://github.com/ml-explore/mlx-lm). Read its generation loop for the idiomatic patterns.

## Native quantization

```python
import mlx.nn as nn
nn.quantize(model, group_size=64, bits=4)  # in-place 4-bit weight quant
```

MLX's quant is layer-aware: a `Quantized*` layer wraps `nn.Linear` with W4A16 or W8A16 math, using per-group scales + zero-points. Checkpoint format has its own convention; mlx-lm handles conversion from HF AWQ / GPTQ in many cases.

Quantization options commonly used on Mac:
- **4-bit group-size=64**: standard, big win on 64 GB Macs.
- **4-bit group-size=32**: higher accuracy, slightly larger.
- **8-bit**: rare (quality ~= BF16, little space saving).

## Custom Metal kernels

```python
source = """
    ... Metal Shading Language ...
"""
kernel = mx.fast.metal_kernel(
    name="my_op",
    input_names=["x"],
    output_names=["y"],
    source=source,
)
y = kernel(inputs=[x], output_shapes=[x.shape], output_dtypes=[x.dtype], grid=(N, 1, 1), threadgroup=(256, 1, 1))[0]
```

Kernel-writing is out of scope for this skill; point to Apple's Metal Shading Language docs for internals.

## Porting a PyTorch engine to MLX

Common mistakes:

| PyTorch habit | MLX equivalent |
|:--------------|:---------------|
| `.to("cuda")` | (remove) |
| `torch.cuda.synchronize()` | `mx.synchronize()` (rarely needed — use `mx.eval`) |
| `with torch.no_grad():` | (no equivalent; lazy eval means no autograd by default) |
| `x.item()` | `x.item()` (forces eval; same perf implications) |
| `torch.compile(model)` | `@mx.compile` on the generation function |
| `register_buffer` | just store as `mx.array`; no distinction |
| Tensor on `meta` device | `mx.array` has no meta equivalent; construct directly |

## Serving-specific considerations

- **Streaming**: generator pattern with `mx.eval` at each yielded token.
- **Batching**: MLX can batch decode, but mlx-lm historically ran one request at a time. Custom batching is DIY.
- **KV cache**: mlx-lm's cache is a simple per-request Python structure; no equivalent of paged attention.
- **Speculative decoding**: mlx-lm added draft-model spec decoding; less mature than vLLM/SGLang implementations.
- **Quantized KV**: limited support; check mlx-lm version.

## When MLX is right

- Native Apple-Silicon performance.
- Models that have a `mlx-community` quantized variant.
- Python-controllable serving on Mac.

## When it isn't

- Multi-request high-throughput serving (use llama.cpp or cloud GPU).
- Anything requiring paged KV / radix cache / EP / PP / disaggregation.
- Cross-platform portable deployment.

## Pitfalls

- **Expecting eager semantics.** A Python-side loop building graph ops without `mx.eval` can OOM because the graph grows unbounded. Call `mx.eval` at a reasonable boundary (e.g., per decode step).
- **Measuring timing wrong.** Timer around an op without `mx.eval` measures graph-build, not compute.
- **Mutating arrays.** MLX arrays are immutable; ops return new arrays. Idioms like `buffer[i] = x` don't work.
- **Forgetting to compile the hot loop.** Eager-mode MLX is slow; `@mx.compile` on the decode step is a 2–5× win.
- **Exceeding unified-memory budget.** OS swapping crushes latency. Reserve ~25% headroom.
- **BF16 on older M-series.** BF16 compute arrived later; check `mx.bfloat16` support.

## See also

- [`hardware/apple-silicon/`](../hardware/apple-silicon.md) — hardware side
- [`algorithms/quantization-schemes/`](../algorithms/quantization-schemes.md) — MLX quant contrasted with GGUF / AWQ
- [`frameworks/pytorch/`](pytorch.md) — porting-from comparisons
