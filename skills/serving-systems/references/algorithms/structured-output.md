# Structured output

Force the model to produce output matching a grammar, regex, or JSON schema by masking logits at each decode step. Orthogonal to prompting — the grammar constrains sampling, not just prompts.

## Mechanism

```
logits ─► (optional penalties) ─► grammar_mask (set disallowed to -inf) ─► sampling
                                       │
                                       └── grammar FSM advances by the sampled token
```

A grammar compiler precomputes a mask (often a bitmap over vocab) per FSM state. At each decode step, the current mask is AND-applied to logits before sampling. The sampled token advances the FSM.

## Library families

| Library | Grammar kind | Speed | Pattern |
|:--------|:-------------|:------|:--------|
| **XGrammar** | CFG (pushdown) + regex | fast (C++ + bitmask) | byte-level, fastest mask compute |
| **Outlines** | regex → FSM, JSON via JSON Schema | moderate | Python-heavy |
| **llguidance** | CFG + regex | fast, Rust | used by Microsoft guidance |
| **lm-format-enforcer** | JSON schema, regex | moderate | early implementation |

Trade: pushdown grammars (CFG) handle nested JSON; regex-only handles flat structures. Most JSON schemas compile fine either way.

## Using XGrammar in an engine

XGrammar is the fastest and most-used grammar library in production serving, so it gets a dedicated how-to. All APIs live in the top-level `xgrammar` namespace.

### Canonical loop

```
TokenizerInfo (once per engine)
   ↓
GrammarCompiler (once per engine; carries a cache)
   ↓
CompiledGrammar (once per distinct grammar)
   ↓
GrammarMatcher (one per active request)
   ↓
per decode step:
   matcher.fill_next_token_bitmask(bitmask, index=i)    # CPU
   → copy bitmask to GPU
   → xgrammar.apply_token_bitmask_inplace(logits, bitmask_gpu, indices=...)
   → sample
   → matcher.accept_token(sampled_id)                   # advance PDA
   → matcher.is_terminated() ?
```

### Core API

| Object | Purpose |
|:-------|:--------|
| `xgrammar.TokenizerInfo` | vocab + metadata; built via `TokenizerInfo.from_huggingface(hf_tokenizer, vocab_size=model_vocab_size, stop_token_ids=...)` (auto-detects `VocabType` and `add_prefix_space`). Manual `TokenizerInfo(encoded_vocab=..., vocab_type=...)` needed for non-HF tokenizers (e.g. Mistral `tekken`). |
| `xgrammar.GrammarCompiler(tokenizer_info, max_threads=8, cache_enabled=True, cache_limit_bytes=-1)` | compiles grammars against this tokenizer; carries an internal cache. `compile_json_schema(schema, any_whitespace=True, strict_mode=..., indent=..., separators=...)`, `compile_builtin_json_grammar()`, `compile_regex(pattern)`, `compile_grammar(ebnf_or_Grammar)`, `compile_structural_tag(tag)`. |
| `xgrammar.CompiledGrammar` | immutable, tokenizer-specific. `.memory_size_bytes`, `serialize_json()` / `deserialize_json(json, tokenizer_info)` for persistence. |
| `xgrammar.GrammarMatcher(compiled_grammar, override_stop_tokens=None, terminate_without_stop_token=False)` | stateful PDA. `accept_token(id) -> bool`, `accept_string(s)`, `fill_next_token_bitmask(bitmask, index=0) -> bool` (False = no masking needed, all tokens allowed), `rollback(n)`, `reset()`, `is_terminated()`, `is_completed()`, `fork()`, `find_jump_forward_string()`. |
| `xgrammar.Grammar` | grammar factory: `from_ebnf(s)`, `from_json_schema(schema)`, `from_regex(pattern)`, `from_structural_tag(tag)`, `builtin_json_grammar()`, `concat(*)`, `union(*)`. Usually you'd just call `compile_*` on the compiler instead. |

### Per-request lifecycle

- **Compile once, match many.** `GrammarCompiler` has a cache — compile identical JSON schemas and you get the cached `CompiledGrammar` back. Reuse the same compiler across all requests.
- **One matcher per request.** Matchers are cheap and stateful. Create on request admission, advance with `accept_token` per sampled token, dispose on finish.
- **Reuse via `reset()`.** vLLM / SGLang pool matchers and call `reset()` when rebinding to a new request with the same grammar.
- **`rollback(n)`** is the speculative-decode-friendly primitive — call it after verify rejects the last `n` tokens. `max_rollback_tokens` constructor arg is deprecated (rollback is now unlimited).

### Batch masking

Bitmask is a shared CPU-side tensor:

```python
bitmask = xgrammar.allocate_token_bitmask(max_num_seqs, vocab_size)
# shape (max_num_seqs, ceil(vocab_size/32)), dtype torch.int32 (= xgrammar.bitmask_dtype)
# bit j of row i set = token j is allowed for request i
```

Each matcher fills its own row: `matcher.fill_next_token_bitmask(bitmask, index=i)`. For a batch of matchers, `xgrammar.BatchGrammarMatcher(max_threads="auto").batch_fill_next_token_bitmask(matchers, bitmask)` parallelizes across CPU threads.

Copy the bitmask to GPU (`.to(device, non_blocking=True)`), then:

```python
xgrammar.apply_token_bitmask_inplace(
    logits,                 # (batch, vocab) on GPU
    bitmask_gpu,            # (batch, ceil(vocab/32)) int32 on GPU
    indices=structured_rows,    # only mask these rows — skip unstructured requests
    backend="auto",             # or "triton" / "cuda" / "torch_compile" / "torch_native"
)
```

`indices` is how you mix structured and free-form requests in one batch — pass the list of rows that have active grammars; unstructured rows are untouched.

### Tokenizer setup — the `vocab_size` gotcha

Always pass the **model's** `vocab_size` (= `lm_head.out_features`), not the tokenizer's `len(tokenizer)`. They differ on Phi-3, DeepSeek-V2, several models with reserved / alignment tokens in the lm_head:

```python
tokenizer_info = xgrammar.TokenizerInfo.from_huggingface(
    tokenizer, vocab_size=model.config.vocab_size, stop_token_ids=[eos_id],
)
```

Getting this wrong masks the wrong logits columns — model outputs garbage.

### Engine wiring (vLLM's pattern)

From `vllm/v1/structured_output/backend_xgrammar.py`, the integration is ~20 lines of glue:

```python
# engine init
tokenizer_info = xgr.TokenizerInfo.from_huggingface(tokenizer, vocab_size=vocab_size)
compiler = xgr.GrammarCompiler(
    tokenizer_info, max_threads=8, cache_enabled=True,
    cache_limit_bytes=VLLM_XGRAMMAR_CACHE_MB * 1024 * 1024,
)
bitmask = xgr.allocate_token_bitmask(max_num_seqs, vocab_size)  # engine-wide buffer

# per request
ctx = compiler.compile_json_schema(schema, any_whitespace=not disable_any_whitespace)
matcher = xgr.GrammarMatcher(ctx)

# per decode step, per structured row i
matcher.fill_next_token_bitmask(bitmask, i)       # CPU
# ... batch-copy bitmask rows to GPU (non-blocking) ...
xgr.apply_token_bitmask_inplace(logits, bitmask_gpu, indices=structured_rows)
# ... sample ...
ok = matcher.accept_token(sampled_id)
if matcher.is_terminated():
    finalize(request_i)

# on speculative reject of n drafted tokens
matcher.rollback(n_rejected)

# on request finish, return matcher to pool
matcher.reset()
```

SGLang's `xgrammar_backend.py` is structurally identical, plus it calls `matcher.find_jump_forward_string()` for jump-forward decoding (see below).

### Jump-forward decoding

When the grammar uniquely determines the next characters — the literal `"name":` after entering an object whose only required key is `name`, the closing `}` of a fully-filled struct, regex literals — there's no point asking the model to predict them. `matcher.find_jump_forward_string()` returns the longest string the current PDA state is forced to emit; tokenize it, accept the ids into the matcher, and skip the corresponding model forwards.

```python
# after each accept_token, before the next forward:
forced = matcher.find_jump_forward_string()
if forced:
    forced_ids = tokenizer.encode(forced, add_special_tokens=False)
    for tid in forced_ids:
        matcher.accept_token(tid)        # advance PDA without sampling
    generated.extend(forced_ids)         # commit to output
    # extend KV cache over forced_ids (one batched forward, discard logits)
    # then resume normal sample-loop
```

When it pays off:
- **Strict JSON schemas with fixed property names** — `Glaiveai2K`-style function-call schemas where `{"name":`, field separators, and `}` are all grammar-forced. Easily half the output tokens.
- **EBNF / regex grammars with literal substrings** — keywords, fixed punctuation, framing.
- **Tool-call protocols** with grammar-pinned framing tokens (`<tool_call>...</tool_call>`, JSON braces).

Pitfalls:
- **Forced string is in characters, not tokens.** Re-tokenize with the same tokenizer used for `TokenizerInfo`. Mismatched tokenization → wrong KV cache, silent corruption.
- **KV cache must absorb the forced ids.** Run one batched forward over `forced_ids` with `use_cache=True` and discard the logits before resuming sampling. Otherwise the next sample step attends to a prefix the model never saw.
- **Speculative decoding interaction.** Run jump-forward first to consume the forced prefix; let spec-decode handle the next free region. Don't try to draft into a forced span — every draft will be a no-op.
- **Sentinel / stop strings.** If you scan output for stop strings or sentinels, include the jumped span in the scan; the model never emitted those tokens but the output contains them.

Engine support: SGLang (in `xgrammar_backend.py`), TRT-LLM partial; vLLM v1 does not (as of writing).

### Capture jump-forward absorb in CUDA graphs (and keep the threshold low)

The KV-absorb forward over `forced_ids` is the perf-critical step of jump-forward. If you run it eager, you trade `L` per-token decode replays (~9 ms each on a graph-captured decode) for `L × ~1-2 ms` of small kernel launches plus a few ms of host overhead — net break-even only kicks in around `L ≥ 6`, and you've left most of the savings on the floor for short forced spans. JSON forced spans are mostly 2-5 tokens (`,"`, `":"`, `"}`, `"name":"`, etc.), so an eager-only path absorbs almost nothing.

**Capture one absorb graph per length.** Pre-capture chunked-extend graphs at startup for the common forced-span lengths — at minimum lengths `2, 3, 4, 5, 6, 7, 8` (or a denser set if your schema produces longer forced spans). Each graph is fixed-shape:

- Static `[1, L]` query tensor for forced ids
- Contiguous absolute `position_ids` / `cache_position`
- Causal mask over `[forced chunk + existing prefix]` against the same KV cache used by the verifier path
- Discardable logits (the forced tokens are already grammar-determined)

Apply the same capture rules as any other captured forward: stable-address tensors, no `.item()` inside the captured region, no internal-scratchpad allocations. Use FlashInfer's batched wrappers with `use_cuda_graph=True` (or SDPA) for the attention call inside the captured graph; do **not** use FlashInfer's `single_prefill_with_kv_cache` — it allocates internal scratchpads that alias under capture and produce divergent logits at the last query position. See `backends/attention-backend-comparison.md`.

**Threshold should be low.** With graph-captured absorb, the per-span cost is ~0.5 ms regardless of `L`, and each absorbed token saves a ~9 ms decode replay. Break-even is ~`L > 0.05`, so the threshold can be **2** (every forced span absorbs). Setting the threshold to 6+ because eager is the only path defeats the optimization for the most common JSON forced spans.

**Differential validation at startup.** For each captured length, run the same forced ids eagerly and via the graph; compare logits per query position. Internal-scratchpad aliasing surfaces only at the last query position, so a per-position max-logit-diff log catches it cleanly. Disable graph absorb only on actual divergence; do not gate on raw replay-time comparisons.

### XGrammar-specific pitfalls

- **Wrong `vocab_size`**: pass model's, not tokenizer's (see above).
- **Compiler is tokenizer-bound**: you must build a new `GrammarCompiler` — and recompile all grammars — when switching tokenizers. `CompiledGrammar.deserialize_json` validates this.
- **Bitmask copy is the hot path at high concurrency**: CPU-fill + H2D per step. Mitigations: pin the bitmask buffer, overlap the copy on a side stream, reuse allocation.
- **`accept_token` returns `False` on violation**: always check. A silent return-False means grammar diverged from model (rare under correct masking, but possible with temperature=0 tie-breaks, wrong vocab_size, or spec-decode bonus tokens).
- **`fill_next_token_bitmask` returns `False` for "no masking needed"**: you can skip the GPU copy for that row when all tokens are allowed — vLLM takes this path.
- **JSON-schema `any_whitespace=True` is the default** but inserts nondeterministic whitespace; flip to `False` for tighter / more predictable output (vLLM exposes `disable_any_whitespace` for exactly this).

## Compatibility with serving features

| Feature | Interaction |
|:--------|:------------|
| Batched sampling | Each request has its own FSM state; masks stacked into `(batch, vocab)` |
| CUDA graph | Mask compute typically CPU-side; graph only captures logits + mask-apply + sample |
| Speculative decoding | Verify must respect the mask; tree-verify needs per-branch FSM snapshots |
| Prefix cache | Two requests with identical FSM state + prompt share KV + safely share first masked-sample decision |
| Tool calling | Usually implemented as "start in free mode, switch to JSON grammar on trigger token" |

## Tool calling

Parsing tool/function calls from model output is structured output's other half. Different model families use different protocols:

| Protocol | Format | Where used |
|:---------|:-------|:-----------|
| OpenAI | JSON in dedicated message field | OpenAI, many fine-tunes |
| Hermes 2/3 | `<tool_call>...</tool_call>` tags | Hermes models, many 2024+ |
| Llama 3.x | `<|python_tag|>` + ipython block | Llama 3.1+ |
| Qwen | `<tool_call>...</tool_call>` or OpenAI-style | Qwen2.5+, Qwen3 |

Engines ship parsers per protocol; `tooling/io-handling/` covers the parser-side detail.

## Compatibility

| Library | vLLM | SGLang | TRT-LLM | Notes |
|:--------|:-----|:-------|:--------|:------|
| XGrammar | ✓ | ✓ | partial | best perf |
| Outlines | ✓ | partial | — | |
| llguidance | ✓ | ✓ | — | |
| lm-format-enforcer | ✓ | — | — | |

## Engine pointers

| Engine | Core path |
|:-------|:----------|
| vLLM | `vllm/v1/structured_output/{backend_xgrammar,backend_outlines,backend_guidance,backend_lm_format_enforcer,backend_types}.py`, `request.py` |
| SGLang | `python/sglang/srt/constrained/` (xgrammar / outlines / llguidance backends) |
| TRT-LLM | XGrammar integration in `_torch/` |

## Pitfalls

- **Token boundary vs. grammar boundary.** The tokenizer rarely aligns with grammar symbol boundaries; the library must work at byte-level or do cross-token matching.
- **Unicode + BPE.** Multi-byte characters span tokens; a byte-level grammar handles this, a string-level one fails.
- **Mask cost.** For large vocabularies (32k–256k), mask compute per step can rival sampling itself. Batch it.
- **Invalid-state recovery.** If somehow the model samples a disallowed token (e.g., with temperature noise on ties), the FSM either errors or recovers by backtracking; pick one behavior and document.
- **Speculative + grammar.** Draft predictions outside the grammar must be rejected, reducing acceptance rate. Either run the grammar on the drafter too or accept the hit.
- **Testing.** Short of a full grammar oracle, test with many prompts and schema edge cases (empty object, missing required field, type coercion).

## See also

- `algorithms/batched-sampling/` — the mask applies before sampling
- `tooling/io-handling/` — tool-call parsing on the output side
- `engines/vllm/` — multi-backend structured output design
