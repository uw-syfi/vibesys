# Continuous Batching

Add continuous batching to an existing single-request LLM inference server so that new requests join the running generation loop without waiting for previous requests to finish.

## Prerequisites

The starting point must already have:
- A model with KV cache support (`past_key_values` in/out)
- A working single-request token-by-token generation loop
- A tokenizer and sampling function

## Model Forward Signature Change

The model's `forward` method must accept **optional** `attention_mask` and `position_ids` so that the batched decode step can override them. When not provided, the model computes them internally (backward-compatible with single-request prefill).

```python
def forward(
    self,
    input_ids: torch.Tensor,
    past_key_values: list[tuple[torch.Tensor, torch.Tensor]] | None = None,
    attention_mask: torch.Tensor | None = None,   # NEW
    position_ids: torch.Tensor | None = None,      # NEW
) -> tuple[torch.Tensor, list[tuple[torch.Tensor, torch.Tensor]]]:
    # Position IDs: use provided or compute from past_len
    if position_ids is None:
        position_ids = torch.arange(past_len, past_len + seq_len, device=...).unsqueeze(0)

    # Attention mask: use provided or compute causal mask for prefill
    if attention_mask is None and seq_len > 1:
        # ... build standard causal mask ...
```

This is necessary because batched decode has different KV cache lengths per request, requiring per-request position IDs and attention masks that cannot be derived from a single `past_len` value.

## Engine Architecture

A background thread runs the generation loop. FastAPI endpoints submit work and await results. 
Key components:
- **`ActiveRequest` dataclass** — tracks per-request state (KV cache, generated IDs, communication primitives)
- **`ContinuousBatchingEngine`** — background thread with the main loop
- **`queue.Queue`** — incoming requests from the async endpoints
- **`asyncio.Future`** / **`asyncio.Queue`** — results back to callers (non-streaming / streaming)

### Main Loop

```
while not stopped:
    1. Drain incoming queue → new_requests
    2. Prefill each new request (single-request forward pass)
    3. Batched decode step (one forward pass for ALL active requests)
    4. Remove finished requests from active list
```

Requests join between decode steps — the queue is drained at the top of each iteration.

## Two-Phase Generation

### Prefill (single-request)

Each new request is prefilled individually because prompt lengths differ. The prefill forward pass builds the initial KV cache and samples the first token.

```python
logits, past_key_values = model(prompt_ids)  # no mask/position override needed
first_token = logits[0, -1, :].argmax()
```

After prefill, the request enters the active list for batched decode.

### Batched Decode

All active requests are decoded in a single forward pass. This requires:
1. Stacking input tokens: `(batch, 1)` — each request's last generated token
2. Per-request position IDs: each request's position = its own KV cache length
3. Padded KV caches: left-padded to the max KV length in the batch
4. Attention mask: `-inf` for padded positions, `0` for real positions


## Token Delivery and Request Lifecycle

### Non-streaming
The endpoint creates an `asyncio.Future`, submits to the engine, and `await`s the future. The engine thread sets the result via `call_soon_threadsafe`:

```python
# Endpoint
req.result_future = loop.create_future()
engine.submit(req)
text, prompt_tokens, completion_tokens, finish_reason = await req.result_future

# Engine thread (on finish)
loop.call_soon_threadsafe(req.result_future.set_result, (text, prompt_len, n_generated, reason))
```

### Streaming
The endpoint creates an `asyncio.Queue` and returns a `StreamingResponse` that reads from it. The engine thread pushes token strings, with `None` as the sentinel:

```python
# Endpoint
req.token_queue = asyncio.Queue()
engine.submit(req)
# StreamingResponse reads from req.token_queue

# Engine thread (per token)
loop.call_soon_threadsafe(req.token_queue.put_nowait, token_text)
# Engine thread (on finish)
loop.call_soon_threadsafe(req.token_queue.put_nowait, None)
```

### Finish conditions
A request finishes when either:
- A generated token is in `eos_ids` → `finish_reason = "stop"`
- `len(generated_ids) >= max_tokens` → `finish_reason = "length"`

Finished requests are removed from the active list at the end of each loop iteration.

## See also

- [`algorithms/async-scheduling/`](async-scheduling.md) — hide the CPU scheduler work (pick next batch, build metadata) behind the GPU forward pass; the next step once continuous batching is working
- [`algorithms/paged-attention/`](paged-attention.md) — the KV-storage substrate that makes variable-length continuous batching efficient
- [`algorithms/chunked-prefill/`](chunked-prefill.md) — interleave long-prompt prefill chunks with active decodes
- [`backends/cuda-graph/`](../backends/cuda-graph.md) — capture the decode step to eliminate kernel-launch overhead
- [`algorithms/batched-sampling/`](batched-sampling.md) — remove per-request CPU-GPU sync on the sampling side


---

## Engine Architecture

## Components

### ActiveRequest Dataclass

Tracks one in-flight generation request through its entire lifecycle:

```python
@dataclass
class ActiveRequest:
    # Identity & parameters
    request_id: str
    prompt_ids: torch.Tensor          # (1, prompt_len) on device
    max_tokens: int
    temperature: float
    top_p: float
    eos_ids: set[int]
    stream: bool

    # Generation state (mutated during prefill/decode)
    past_key_values: list[tuple[torch.Tensor, torch.Tensor]] | None = None
    generated_ids: list[int] = field(default_factory=list)
    seq_len: int = 0
    finished: bool = False
    finish_reason: str = "stop"
    prompt_len: int = 0

    # Communication back to caller
    result_future: asyncio.Future | None = None     # non-streaming
    token_queue: asyncio.Queue | None = None        # streaming
    event_loop: asyncio.AbstractEventLoop | None = None
```

Key design choices:
- `past_key_values` is per-request and unpadded — padding only happens just-in-time for batched decode
- `event_loop` reference is needed to safely push results from the background thread
- `prompt_ids` is kept on the GPU device (tokenized once at submission time)

### ContinuousBatchingEngine

Owns a background daemon thread and the active request list:

```python
class ContinuousBatchingEngine:
    def __init__(self, model, tokenizer, device):
        self._incoming: queue.Queue[ActiveRequest] = queue.Queue()
        self._active: list[ActiveRequest] = []
        self._thread: threading.Thread
        self._stop_event: threading.Event
```

## Thread-Async Bridge

The engine thread and the FastAPI async event loop run on different threads. Communication uses thread-safe primitives:

### Submission (async → thread)

`queue.Queue` is thread-safe by default. The endpoint calls `engine.submit(req)` which does `self._incoming.put(req)`.

### Results (thread → async)

`asyncio.Future` and `asyncio.Queue` are **not** thread-safe. The engine thread must use `call_soon_threadsafe` to push results:

```python
# From the engine thread:
req.event_loop.call_soon_threadsafe(req.result_future.set_result, result)
req.event_loop.call_soon_threadsafe(req.token_queue.put_nowait, token_text)
```

This schedules the operation on the event loop's thread, avoiding race conditions.

## Main Loop Detail

```python
def _loop(self):
    while not self._stop_event.is_set():
        # 1. Drain incoming queue (non-blocking)
        new_requests = []
        try:
            while True:
                new_requests.append(self._incoming.get_nowait())
        except queue.Empty:
            pass

        # 2. Prefill new requests individually
        for req in new_requests:
            self._prefill(req)
            self._active.append(req)

        # 3. Sleep if idle
        if not self._active:
            time.sleep(0.001)
            continue

        # 4. Batched decode
        self._decode_step()

        # 5. Prune finished requests
        self._active = [r for r in self._active if not r.finished]
```

### Why drain non-blocking?

Using `get_nowait()` in a loop ensures we pick up all requests that arrived since the last iteration without blocking the decode step. If we used `get(timeout=...)`, we'd add unnecessary latency when there are active requests waiting to decode.

### Why `time.sleep(0.001)` when idle?

Without a brief sleep, an empty loop would spin the CPU at 100%. The 1ms sleep balances responsiveness (new requests are picked up within ~1ms) with CPU efficiency.

## Prefill Phase

Each new request is prefilled individually because prompt lengths vary:

```python
@torch.inference_mode()
def _prefill(self, req):
    logits, past_key_values = self.model(req.prompt_ids)  # single request, no mask override
    req.past_key_values = past_key_values
    req.prompt_len = req.prompt_ids.shape[1]
    req.seq_len = req.prompt_len

    # Sample first token
    token_id = sample(logits[0, -1, :])
    req.generated_ids.append(token_id)
    req.seq_len += 1

    if token_id in req.eos_ids:
        req.finished = True
        self._finish_request(req)
        return

    self._deliver_token(req, token_id)
```

The prefill uses the model's default mask/position computation (no override needed) because it's a single request with a standard causal setup.

## Decode Phase

(See the **Kv Cache Batching** section below for the detailed KV cache padding strategy.)

After the batched forward pass:
1. Extract per-request KV caches (removing padding)
2. Sample next token per request
3. Check finish conditions per request
4. Deliver token or finish

## Endpoint Integration

Endpoints create an `ActiveRequest`, submit it to the engine, and await the result:

```python
@app.post("/v1/completions")
async def completions(req):
    loop = asyncio.get_event_loop()
    active_req = ActiveRequest(
        ...,
        result_future=loop.create_future(),  # or token_queue=asyncio.Queue() for streaming
        event_loop=loop,
    )
    engine.submit(active_req)

    # Non-streaming: await the future
    text, prompt_tokens, completion_tokens, finish_reason = await active_req.result_future

    # Streaming: return StreamingResponse that reads from token_queue
```

No `asyncio.Lock` is needed — the engine serializes all GPU access on its own thread.

## Lifecycle and Cleanup

- **Server startup** (lifespan): create engine, call `engine.start()`
- **Server shutdown** (lifespan): call `engine.stop()`, which sets `_stop_event` and joins the thread
- **Request completion**: engine sets future/queue, removes from `_active` list. GPU memory (KV cache tensors) is freed when the `ActiveRequest` is garbage-collected


---

## Kv Cache Batching

The core challenge: each active request has a KV cache of different length. To run a single batched forward pass, all KV caches must have the same sequence dimension.

This file covers the **pad-and-stack baseline**, which is the simplest correct strategy and the right starting point for a from-scratch engine. Production engines do not ship this approach — they use variable-length packing (FlashAttention) or paged KV cache (FlashInfer, vLLM PagedAttention). Both are summarized at the end under "Beyond pad-and-stack".

## Left-Padding

KV caches are **left-padded** with zeros so that real tokens align at the right edge. This matters because the new decode token is always appended at the end (rightmost position), and the attention mask must contiguously mask the left side.

```
Request A (past_len=5): [0 0 0 | a a a a a]   ← 3 pad + 5 real
Request B (past_len=8): [b b b b b b b b]       ← 0 pad + 8 real
max_past_len = 8
```

### Pad and Stack

For each layer, left-pad each request's K and V to `max_past_len`, then `torch.cat` along the batch dimension:

```python
batched = []
for layer_idx in range(num_layers):
    k_list, v_list = [], []
    for i, req in enumerate(active):
        k_i = req.past_key_values[layer_idx][0]  # (1, num_kv_heads, past_len_i, head_dim)
        v_i = req.past_key_values[layer_idx][1]
        pad_len = max_past_len - past_lens[i]
        if pad_len > 0:
            k_pad = torch.zeros(1, num_kv_heads, pad_len, head_dim, dtype=k_i.dtype, device=device)
            v_pad = torch.zeros(1, num_kv_heads, pad_len, head_dim, dtype=v_i.dtype, device=device)
            k_i = torch.cat([k_pad, k_i], dim=2)  # left-pad
            v_i = torch.cat([v_pad, v_i], dim=2)
        k_list.append(k_i)
        v_list.append(v_i)
    batched.append((torch.cat(k_list, dim=0), torch.cat(v_list, dim=0)))
```

## Attention Mask

Shape: `(batch_size, 1, 1, max_past_len + 1)` — one query token attending to all past + itself.

- `0` for real token positions (attend)
- `-inf` for padded positions (ignore)

```python
total_len = max_past_len + 1
attention_mask = torch.zeros(batch_size, 1, 1, total_len, device=device)
for i, pl in enumerate(past_lens):
    pad_len = max_past_len - pl
    if pad_len > 0:
        attention_mask[i, 0, 0, :pad_len] = float("-inf")
attention_mask = attention_mask.to(dtype=model_dtype)  # e.g. float16
```

The mask broadcasts over all attention heads. Since softmax is computed in float32, `e^(-inf)` is exactly 0 — padded positions contribute nothing to the output.

## Position IDs

Each request's decode token has position = its own `past_len` (the KV cache length before this step), regardless of other requests in the batch:

```python
position_ids = torch.tensor([[pl] for pl in past_lens], dtype=torch.long, device=device)
```

This is correct because RoPE encodes absolute position, and each request has its own sequence of positions.

## Unpadding After Forward Pass

After the batched forward pass, the model returns KV caches of shape `(batch, heads, max_past_len + 1, dim)`. Extract per-request caches by removing left-padding:

```python
for i, req in enumerate(active):
    pad_len = max_past_len - past_lens[i]
    new_kv = []
    for layer_idx in range(num_layers):
        k_full = new_past_key_values[layer_idx][0][i:i+1]
        v_full = new_past_key_values[layer_idx][1][i:i+1]
        k_real = k_full[:, :, pad_len:, :]   # remove left padding
        v_real = v_full[:, :, pad_len:, :]
        new_kv.append((k_real.contiguous(), v_real.contiguous()))
    req.past_key_values = new_kv
```

The `.contiguous()` ensures the sliced tensor has clean memory layout for the next iteration.

## Why Not Right-Padding?

Right-padding would place zeros after real tokens. This would require the attention mask to have a "hole" in the middle (mask right-pad, then attend to the new token appended after the pad). Left-padding keeps the mask simple: contiguous `-inf` on the left, contiguous `0` on the right + new token.

## Memory Considerations

Each decode step re-pads all KV caches. For `N` active requests with `L` layers, the peak memory for padding is:

```
N × L × 2(K+V) × max_past_len × num_kv_heads × head_dim × dtype_size
```

This is the same as storing one full-length copy per request (the padded version) on top of the per-request unpadded caches. The unpadded caches are kept to avoid accumulating padding waste across steps.

---

# Beyond pad-and-stack

The approach above is correct and easy to reason about, but it scales poorly:

- **Wasted memory.** Every request is padded to `max_past_len`, so one 32k-token request forces every other request in the batch to reserve 32k-long K/V tensors per layer.
- **Wasted compute.** Attention runs on the full `max_past_len + 1` dimension; the `-inf` masked positions contribute nothing but still cost FLOPs and HBM traffic.
- **Allocator churn.** Every step re-pads, re-allocates, and re-frees the padded tensors.
- **Long-context falls over.** At 128k context, the padded baseline OOMs at a single-digit batch size on an 80GB GPU.

Production engines use one of two replacement strategies. Both remove padding entirely.

## Option 1: variable-length packing (FlashAttention family)

Pack every active request's tokens into **one flat tensor**, with an `int32` prefix-sum array marking per-request boundaries. The attention kernel does per-request masking internally from those offsets.

- Memory: proportional to the **sum** of sequence lengths, not `batch × max_seq_len`.
- Compute: zero wasted FLOPs on padded positions.
- Fits decoder-only causal attention naturally; sliding-window and local masks via `window_size`.

### Prefill / chunked prefill (variable-length queries)

```python
from flash_attn import flash_attn_varlen_func

# Packed Q, K, V for all requests' prompt tokens:
#   q: (total_q_tokens, num_q_heads, head_dim)
#   k: (total_k_tokens, num_kv_heads, head_dim)
#   v: (total_k_tokens, num_kv_heads, head_dim)
# cu_seqlens_q, cu_seqlens_k: (batch+1,) int32 prefix sums
out = flash_attn_varlen_func(
    q, k, v,
    cu_seqlens_q, cu_seqlens_k,
    max_seqlen_q, max_seqlen_k,
    causal=True,
)
```

### Decode with paged KV (in-place append + attention)

```python
from flash_attn import flash_attn_with_kvcache

out = flash_attn_with_kvcache(
    q=q_step,                          # (batch, 1, num_q_heads, head_dim)
    k_cache=k_cache,                   # (num_blocks, page_block_size, num_kv_heads, head_dim)
    v_cache=v_cache,
    k=k_step, v=v_step,                # new K/V to append in place
    cache_seqlens=cache_seqlens,       # (batch,) int32
    block_table=block_table,           # (batch, max_blocks_per_seq) int32
    causal=True,
)
```

`page_block_size` must be divisible by 256. All metadata tensors are `torch.int32`. The engine owns the block pool and the per-request block list; FlashAttention just consumes the layout.

For CUDA graphs, static metadata tensor shapes are necessary but not sufficient. Validate the exact FlashAttention version and kernel path before assuming a graph captured at `cache_seqlens=N` can be replayed at `N+1`; local FA2 probes replayed the shorter effective length. The safe choices are eager/piecewise attention, or full capture by fixed length bucket.

**Full coverage, including the paged-KV contract and a minimal KV manager**: [`backends/flashattention/SKILL.md`](../backends/flashattention.md) and its [`#paged-kv-manager`](../../../backends/flashattention/#paged-kv-manager).

## Option 2: paged KV with plan/run wrappers (FlashInfer)

Non-contiguous block-based KV storage addressed by a per-request page table. Uses a "plan once per batch, run once per layer" pattern.

```python
import flashinfer

workspace = torch.zeros(128 * 1024 * 1024, dtype=torch.uint8, device="cuda")
prefill = flashinfer.BatchPrefillWithPagedKVCacheWrapper(workspace, "NHD")
decode  = flashinfer.BatchDecodeWithPagedKVCacheWrapper(workspace, "NHD")

# Once per batch step:
decode.plan(
    indptr=kv_indptr,            # (batch+1,) prefix-sum of pages per request
    indices=kv_indices,          # (total_pages,) flat list of physical page IDs
    last_page_len=kv_last_page_len,  # (batch,)
    num_qo_heads=..., num_kv_heads=..., head_dim=..., page_size=...,
)

# Append new K/V into paged storage:
flashinfer.page.append_paged_kv_cache(
    k_step, v_step, batch_indices, positions,
    kv_cache, kv_indices, kv_indptr, kv_last_page_len, layout="NHD",
)

# Once per layer:
out = decode.run(q, kv_cache)
```

### MLA variants (DeepSeek-class models)

```python
mla = flashinfer.mla.BatchMLAPagedAttentionWrapper(workspace, "NHD")
# plan() / run() with compressed-KV representation
```

### Typical cache tensor

```
kv_cache: (max_pages, 2, page_size, num_kv_heads, head_dim)   # per layer
#                      ^ 0 = K, 1 = V
```

**Full coverage of wrappers, layouts, and pitfalls**: [`backends/flashinfer/SKILL.md`](../backends/flashinfer.md).

## Picking between them

| Concern | Variable-length (FlashAttention) | Paged (FlashInfer) |
|:--------|:---------------------------------|:-------------------|
| Prefill (variable prompt lengths) | `flash_attn_varlen_func` | `BatchPrefillWithPagedKVCacheWrapper` |
| Decode with paged KV | `flash_attn_with_kvcache` with `block_table` | `BatchDecodeWithPagedKVCacheWrapper` |
| MLA / compressed KV | not supported — use MLA-specific backend | `flashinfer.mla.*` wrappers |
| Plan/run pattern | no explicit plan step; kernel reads metadata directly | explicit `plan()` per batch, separate workspace |
| Engine drives per-layer loop | yes (append + attention per layer) | yes (append + `run()` per layer) |
| Minimum block size | `page_block_size % 256 == 0` | `page_size` user-defined (commonly 16) |
| CUDA graph compatibility | eager/piecewise by default; full graph only for fixed/bucketed effective lengths, not arbitrary growing `cache_seqlens` | yes with dedicated per-bs wrapper and static buffers; `plan()` stays outside capture |

Both require the engine to **drive the per-layer loop directly** — model classes become weight containers, and the engine orchestrates `layernorm → QKV → RoPE → append KV → attention → output` per layer. See [`backends/flashinfer/SKILL.md`](../backends/flashinfer.md) "Engine-drives-layers architecture" for the full pattern.

## Related skills

- [`algorithms/paged-attention/`](paged-attention.md) — the block-pool / page-table design that underlies both options
- [`algorithms/radix-prefix-caching/`](radix-prefix-caching.md) — built on top of paged KV, shares prefixes across requests
- [`backends/flashattention/`](../backends/flashattention.md) — variable-length + paged-via-block_table API
- [`backends/flashinfer/`](../backends/flashinfer.md) — plan/run wrappers + MLA variants
- [`algorithms/chunked-prefill/`](chunked-prefill.md) — uses the varlen path for interleaved prefill + decode batches
