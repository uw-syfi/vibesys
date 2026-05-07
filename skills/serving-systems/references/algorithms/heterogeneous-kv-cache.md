# Heterogeneous KV cache

When different layers in a model need different per-token memory (full attention vs sliding window vs SSM state vs vision embedding), a single page size wastes memory and a single eviction policy wastes hits. Jenga ([arXiv 2503.18292](https://arxiv.org/abs/2503.18292)) formalizes the design space as **two orthogonal decisions**:

1. **Allocator**: how physical pages are sized and recycled across layer types.
2. **Prefix cache policy**: what counts as a "hit" per layer and how eviction stays coordinated.

This skill describes that design. Treat the names (`PageGroup`, `LayerSupportsPrefixCache`, "aligned eviction") as a vocabulary — engines that adopt the design rarely use the same identifiers.

## Prerequisites

- `algorithms/paged-attention/` — block-based KV, page tables, block pool.
- `algorithms/radix-prefix-caching/` — prefix sharing on a paged pool.
- `models/ssm-hybrid/` — why SSM layers cache a fixed state, not per-token KV.
- `algorithms/attention-variants/` — sliding-window, chunked-local, MLA, cross-attention.

## The heterogeneity problem

Per-token memory footprint varies by layer type:

| Layer type | Per-token bytes (illustrative) | Cacheable prefix |
|:---|:---|:---|
| Full attention (MHA/GQA) | `2 * num_kv_heads * head_dim * dtype` | every token |
| MLA | compressed latent — much smaller | every token |
| Sliding-window / chunked-local | same as full, but only last W useful | last W tokens |
| Mamba-2 SSM | fixed state (no per-token growth) | only the final state |
| Vision embedding cache | per-image, not per-token | per image |

A naive allocator picks one strategy:

| Strategy | Consequence |
|:---|:---|
| **MAX**: one page size = max across types | up to `max / min` × internal fragmentation in small-footprint layers |
| **GCD**: one page size = gcd | kernels must loop over many tiny pages; kernel-side changes |
| **Per-type pool**: separate pool per layer type | external fragmentation — one pool full while others idle; memory can't flow |

None of these lets a prefix-cache hit in full-attention coexist cleanly with a hit in a sliding-window or SSM layer.

## Design 1 — Two-level LCM page allocator

**Large page** = least common multiple of all layer types' per-token page sizes × block_size. **Small page** = the per-layer-type page sized to that layer's embedding.

```
large page P  (size = LCM(a, b, c) for layer types a, b, c)
  ├── small pages for type-a layers  (P / a of them)
  ├── small pages for type-b layers  (P / b of them)
  └── small pages for type-c layers  (P / c of them)
```

- The allocator recycles large pages. Small pages are the unit of logical assignment to requests.
- Small pages within one large page share physical memory — when the large page is reclaimed, all its small pages go with it.
- No kernel changes: each layer's kernel sees a normal page table of small pages of its own size.

### Request-aware small-page placement

To keep large pages reclaimable, fill small pages within a single large page from the **same** request when possible. Allocation priority:

1. Unused small page in a large page that already hosts this request.
2. Empty large page → carve fresh small pages for this request.
3. Evict an evictable large page (LRU across large pages).
4. Reuse unused small pages from other requests (fragments the large page).
5. Evict an individual small page (last resort — now the large page is heterogeneous and harder to reclaim).

Without this, small pages from different requests interleave in large pages and large pages never become fully reclaimable.

### Large-page state machine

| State | Condition |
|:---|:---|
| **Empty** | every small page empty (no valid cache, no in-use) |
| **Evictable** | every small page evictable (has cache, no in-use) |
| **Used** | any small page is in use |

Reclaim in order: empty first, then evictable (LRU by the latest last-access among its small pages). Mixed-state large pages are skipped by the large-page evictor — they fall through to small-page eviction.

### Memory layout

PagedAttention's original layout is **layer-page** — partition by layer, then by page within each layer. Switching to **page-layer** — partition by (large) page, then layers within each page — is what makes inter-type sharing physical. Each small page remains contiguous; the change is only in how the flat KV arena is subdivided.

## Design 2 — Customizable prefix caching with aligned eviction

Each layer type implements three hooks. Call the interface `LayerSupportsPrefixCache`.

### Hook 1: `update_last_access(request, time)`

Decides which small pages' last-access timestamps get touched when the request runs. Per-layer policy:

| Layer type | Tokens that get touched |
|:---|:---|
| Full attention | all tokens in the request |
| Sliding-window (W) | only tokens in `[seq_len - W, seq_len)` |
| SSM | only the final state |
| Vision embedding | only the image(s) attended this step |

This is the **balanced eviction** invariant: for a token that matters to a layer, the layer's small page keeps a fresh timestamp; for a token that doesn't matter, the timestamp stays old and the page evicts first. Without per-layer hooks, a shared LRU keeps useless pages alive.

### Hook 2: `set_prefix_length(request)`

Assigns a per-small-page integer priority (typically the token position at the end of the block, or the image's global ordering). Used as a **tiebreaker** when many pages share the same `last_access` — evict smaller `prefix_length` first within a group. The invariant across layer types: **the same logical token gets the same `prefix_length` value in every layer** so that when eviction sweeps by `(last_access, prefix_length)` the surviving set of tokens is the same across layers — the **aligned eviction** property.

### Hook 3: `get_possible_prefix(request, is_hit)`

Returns the set of prefix lengths that form a valid cache hit for this layer, given which of its blocks are currently cached. Per-layer policy:

| Layer type | Valid prefix lengths |
|:---|:---|
| Full attention | any contiguous prefix from the left |
| Sliding-window (W) | any `L` such that `[L-W, L)` is all cached |
| SSM | multiples of the SSM-cache stride (e.g. every N tokens, if caching sparsely); otherwise only the last-state position |
| Vision embedding | any set of fully-cached images |

The coordinator's prefix-cache hit for a request is the **longest L that is valid in every layer type**. The valid sets shrink right-to-left as layer constraints are intersected.

### Why aligned eviction matters

Without alignment, layer-A evicts token 1000's page while layer-B still has it — the prefix now hits up to 999 in A, up to, say, 1500 in B, and the coordinator can only use 999. The tokens from 1000–1500 in B occupy memory without contributing to hits. Aligned eviction forces the intersection to stay tight: when a token leaves one layer's cache, it leaves the others' too, and the freed memory is reusable.

## Per-layer-type customizations

| Layer | `update_last_access` | `set_prefix_length` | `get_possible_prefix` |
|:---|:---|:---|:---|
| Full attention | every token | token index | `[0, cached_prefix_len]` |
| Sliding-window W | tokens in `[L-W, L)` | token index | `{L : [L-W, L) all cached}` |
| Chunked-local | tokens in current chunk | token index | `{L : current chunk cached}` |
| SSM (sparse, every N) | last cached token only | floor(token / N) × N | `{k·N : k·N cached}` |
| MLA | every token | token index | same as full attention |
| Vision embedding | tokens of attended images | **random value per image** | `{L : all images in prefix cached}` |

The vision case is the most unusual — assigning a **random** `prefix_length` per image gives each image an independent eviction lottery, avoiding "last image always evicts first" pathologies when encoder cost dominates and images repeat across requests.

## Workflow for adopting this design

1. **Declare a `KVCacheSpec` per layer type** — page size, block size, per-layer policy class.
2. **Compute the compatible large-page size** — LCM of per-type page sizes. Refuse models where the LCM is pathologically large (> a few MB per large page).
3. **Allocate one flat arena**; partition into large pages; inside each large page, lay out per-type small-page slots page-layer style.
4. **Scheduler admission path**: for each admitted request, call `get_possible_prefix` on every layer type, intersect, take the max valid L; this is the prefix hit.
5. **Per-step runtime**: call `update_last_access` and `set_prefix_length` on the running requests' touched pages.
6. **Eviction path**: try empty large pages → evictable large pages (LRU over large pages by max child last-access) → small-page LRU `(last_access, prefix_length)` as fallback.

## Compatibility

| Layer combination | LCM page viable? | Gotcha |
|:---|:---|:---|
| Full + sliding-window | yes — same per-token bytes, only hit policy differs | `update_last_access` must restrict SW to the window |
| Full + MLA | LCM explodes if dtypes differ (e.g. fp8 attn + bf16 MLA) | quantize uniformly or accept MAX fallback |
| Full + SSM (Mamba) | SSM is per-request, not per-token — pad SSM state up to a multiple of attention page size | SSM prefix cache is optional (Jenga caches every N tokens) |
| Attention + vision encoder cache | works, but vision cache is image-granular | use random `prefix_length` per image |
| Full + chunked-local (Llama-4) | yes | chunk boundary ≠ page boundary → mind alignment |

## Pitfalls

- **LCM blow-up.** If per-type page sizes are coprime and large (e.g. 384 and 512), LCM is their product. Check at config time; either quantize layouts to common factors or fall back to MAX for outlier layers.
- **Forgetting aligned `prefix_length`.** Two layer types can both implement the interface correctly but assign prefix lengths on different scales (e.g. token index vs block index). Tiebreak comparisons then mean nothing. Pick one scale (usually token index) and stick to it.
- **Small-page-evict cascade.** Step 5 of the allocator fragments large pages. If hit rate tanks, most large pages become mixed-state and unreclaimable. Monitor the fraction of large pages in each state; if mixed > ~10%, rethink request-aware placement or increase arena size.
- **SSM "every N tokens" cache coherency.** If you cache SSM state every N tokens, a prefix hit at length L is only valid when `L mod N == 0` **and** the state at `L` survived eviction. Don't accept partial hits — the state depends on the whole preceding sequence.
- **Vision random-priority instability.** Re-rolling the random value on each admission thrashes the cache; fix the value per image for its cache lifetime.
- **Scheduler assumes uniform block size.** Many schedulers compute token budgets as `num_blocks * block_size`. With heterogeneous small-page sizes, pick the LCM's tokens-per-large-page as the scheduler's unit and let each layer divide it internally.
- **Request-aware placement ≠ correctness.** It's an efficiency heuristic. A correct implementation still works if every new request takes a fresh large page; it just wastes memory.

## See also

- [`algorithms/paged-attention/`](paged-attention.md) — the single-type allocator this generalizes
- [`algorithms/radix-prefix-caching/`](radix-prefix-caching.md) — trie structure that sits above `get_possible_prefix`
- [`models/ssm-hybrid/`](../models/ssm-hybrid.md) — the concrete motivation for SSM-state cache
- [`algorithms/attention-variants/`](attention-variants.md) — per-variant hit policies (SWA, chunked-local, MLA, cross)
- Jenga paper: [arXiv 2503.18292](https://arxiv.org/abs/2503.18292)
