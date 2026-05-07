# Speculative decoding

Cheaply propose `k` candidate tokens, verify them in **one** target-model forward pass, keep the longest accepted prefix. When the draft matches well, the target runs fewer decode steps; when it doesn't, the verify still costs only a single target forward. The trade is per-call work (a slightly wider verify forward + a small drafter forward) for fewer total target calls.

The dominant variants are:

| Variant | Drafter | Vocab | Notes |
|:--|:--|:--|:--|
| **Draft-model** | A separate small full LM (e.g. Qwen3-0.6B drafting for Qwen3-32B). | Often same vocab → no projection needed. | Simplest to implement. Drafter has its own KV cache and graph capture. |
| **MTP** | Multi-token-prediction heads attached to the target. | Same as target. | Single forward produces target + draft logits; no separate drafter model. |
| **Medusa** | Multiple LM heads on top of the target. | Same as target. | Heads predict different relative positions in parallel. |
| **EAGLE-3** | A one-layer model that conditions on target auxiliary hidden states. | Compressed sub-vocab; needs `d2t` / `t2d` projection between drafter and verifier ids. | Best acceptance per drafter cost; most production-deployed. |
| **n-gram / suffix** | Look up recent context. | Same as target. | Cheap, low acceptance, useful for highly-repetitive prompts. |

The structural lessons below — verify in one target forward, capture every phase as a CUDA graph, GPU-resident accept length — apply to all variants. Sections about vocab projection apply to EAGLE-3 only.

## The 1-forward verify recipe

Most spec-decode wins come from this — verifying `k+1` tokens in **one** target forward, not `k+1` separate forwards.

1. Drafter produces `[d_1, d_2, …, d_k]`.
2. Build the verifier input as `prompt_kv + [base, d_1, d_2, …, d_k]`, where `base` is the verifier-sampled token from the previous step.
3. Run **one** target forward over those `k+1` query positions with the standard causal mask. Returns `k+1` logit vectors.
4. For each position `i ∈ [0, k-1]`: accept iff `argmax(target_logits[i]) == d_{i+1}`. Stop on the first mismatch. Position `k` always gives a free bonus token.
5. Append the accepted prefix (length `0..k`) plus the bonus token. Truncate verifier KV to `prefill_len + accept_length + 1` (drop KV of rejected drafts).
6. Update drafter KV by the same accepted length.

If your code does `k+1` separate target forwards, you've gained nothing.

## Drafter must run incremental decode (not full prefill)

The single most common perf bug in a freshly-wired draft-model implementation: the drafter forward is implemented as a fresh forward over the entire growing context. Pseudo-code of the antipattern:

```python
draft_prefix = full_prompt_ids + emitted_so_far     # grows every emitted token
for _ in range(k):
    logits = self.draft_model(draft_prefix).logits[:, -1, :]   # full forward, no KV reuse
    draft_prefix = torch.cat([draft_prefix, [argmax(logits)]], dim=1)
```

Even at modest context (~100 tokens) with a 0.6B drafter this costs tens of milliseconds per draft step — the drafter ends up consuming more time than the target verify it was meant to save, and end-to-end throughput regresses below verifier-only.

The drafter needs the same machinery the target already has:

- Its own paged-KV cache (same FlashInfer wrappers, same persistent workspace/index buffers).
- A captured one-token decode graph (replay per draft step is launch-bound, exactly like the target).
- Prompt prefill once at request start, then incremental advance only over emitted tokens.

If the drafter shares the target's tokenizer and vocab (Qwen3 0.6B + 32B), the drafter and target read the *same* token id stream, so prompt prefill is one shared pass conceptually but the drafter still needs its own KV state.

## Verify-extend uses the same backend, never a parallel eager path

A second common bug: writing a fresh `verify_extend(input_ids)` that uses manual `F.scaled_dot_product_attention` + a hand-built attention mask, separate from the captured decode path. Side effects:

- Verify itself is eager: every k+1 query position pays full Python + kernel launch overhead. At 87 ms per call (observed), verify alone is heavier than the entire target step it replaces.
- Branching `_generate_sync` to call `verify_extend` instead of the captured decode replay can disable the captured target decode graph entirely. Profiles after a botched spec wiring often show `cuda_graph_replays = 0` even on non-spec requests, because the runner's graph state never gets warmed.

Verify must run through FlashInfer's **batched** wrappers with `use_cuda_graph=True`, with one captured graph per `(decode bucket, k)` shape. The non-spec fallback decode path must keep using its own captured graph; the only branching is choosing which graph to replay.

## Capture every phase as a CUDA graph

A standard "one big decode graph" capture does NOT work — the phases have different shapes. Even for the simple draft-target case you need three captures, plus the non-spec target decode:

| Graph | Shape (per replay) | Indexed by | Used by |
|:------|:-------------------|:-----------|:--------|
| **Target decode** | one query token, verifier KV | per decode bucket | non-spec fallback / first base-token step |
| **Drafter decode** | one query token, drafter KV | per decode bucket | each of the `k` draft steps |
| **Verify-extend** | `k+1` query tokens, verifier KV | per `(decode bucket, k)` | the single verify forward |
| **Draft-extend** (EAGLE / tree variants) | up to `accept_length+1` query tokens, drafter KV | per `(decode bucket, max accept_length)` | post-verify drafter catch-up |

For draft-target with same-vocab drafter (no tree), only the first three are required.

Capture-and-replay rules — apply to every graph:

- All input tensors live at **fixed device addresses** for the lifetime of the graph (preallocate during startup; in-place `copy_` to refresh per call).
- No `.item()`, no Python branching on tensor values, no dynamic-shape `slice` against a Python int *inside* the captured forward.
- Causal masks / position IDs are preallocated and the captured forward indexes them — no per-call mask allocation.
- Use a graph-friendly attention backend (see `backends/attention-backend-comparison.md`). FlashInfer's batched wrappers are the typical choice; FlashInfer's single-request wrappers and SDPA-with-dynamic-shape break capture.

Static-vs-graph differential validation at startup: run each forward eagerly, then via the captured graph on the same inputs, and compare logits before serving any traffic. Off-by-N-byte aliasing of internal scratchpads typically surfaces only at the *last* query position; a per-position max-logit-diff log catches it cleanly.

## Grammar interaction

Two correctness invariants when xgrammar (or any structured-output mask) is active:

1. **Mask the verifier at every `k+1` position, including the bonus.** The drafter terminating the grammar early (`accept(token)` returning a final state mid-draft) is a common path. If the post-loop bonus mask only gets appended on the "drafter did not terminate" branch, the bonus position runs unmasked and its argmax can emit any token — including a sentinel string in a numeric field.
2. **Roll back KV state by the same length.** Truncating only `seq_len = old + 1 + accept_len` is not enough if any code path indexes K/V using a different length tracker (FlashInfer page metadata, eager verify-extend recomputing keys with `key_positions[:new_seq_len]`, etc.). Reset all of them. Stale rejected K/V leaks corrupt the next decode's attention.

**Should you mask the drafter logits too?** Depends on the drafter:

- **Same-vocab drafter (Qwen3-0.6B + Qwen3-32B):** masking both is fine and slightly increases acceptance because draft proposals stay grammar-legal. Apply the same bitmask to drafter and verifier logits — there's no projection cost.
- **Compressed-vocab drafter (EAGLE-3 with d2t):** generally do NOT mask the drafter. The compressed draft vocab (~32k of the target's ~128k) collapses badly under a tight grammar mask: the verifier may have ~5 allowed target tokens, only 1-2 with a draft-vocab equivalent, and the drafter is forced to pick from a tiny mapped set, often missing the verifier's actual choice. The verifier's mask alone provides the correctness guarantee — rejected proposals just fail acceptance.
- Either way: skip the speculative step entirely when the grammar mask leaves zero candidates.

**Don't paper over schema issues with post-hoc validation.** A "validate the emitted text against the schema, retry without spec on failure" wrapper is not a correctness mechanism — judges (and reviewers) will reject it. Schema validity must come from per-token grammar masking + matcher advancement, both inline.

xgrammar's `find_jump_forward_string` (see `algorithms/structured-output.md`) is a complementary lever: it consumes always-determined spans (e.g. `"name":` after the previous token forces those bytes) without involving the drafter. On JSON workloads this often delivers more throughput than spec decoding alone, with much less implementation risk.

## Debugging 0 acceptance

Wired the drafter, verifier returns logits, draft KV updates — but `accept_length` is 0 every step. Check in this order:

1. **KV desync after rejection.** Drafter KV must advance by `accept_length`, not by `k`. If it advances by `k` regardless, its hidden state for step `t+1` conditions on rejected tokens. Symptom: first request matches occasionally, every later request stays at 0.
2. **Off-by-one in the verify input.** The verifier input must be `[base, d_1, …, d_k]`, where `base` is the previous verifier-sampled token; verification compares `argmax(target_logits[i])` against `d_{i+1}`. Comparing against `target_logits[base_position]` shifts every position by one → 0 acceptance.
3. **Drafter prompt-prefill not done.** If the drafter starts from an empty KV at request time, every drafter forward sees only the partial context and proposes effectively random tokens.
4. **EAGLE-3 specific:** wrong aux-layer indices, pre-final-norm vs post-final-norm mix-up, or `d2t` mapping skipped/applied twice. See the EAGLE-3 contract below.

A 5-minute differential log catches almost all of these:

```python
# For draft-target (same vocab):
target_argmax = target_logits.argmax(dim=-1)            # [k+1]
draft_argmax = draft_logits.argmax(dim=-1)              # [k]
for i in range(k):
    print(f"step={step} pos={i} draft={draft_argmax[i].item()} "
          f"target_at_i+1={target_argmax[i+1].item()} "
          f"match={draft_argmax[i].item() == target_argmax[i+1].item()}")
```

Get acceptance > 0 in greedy mode first. Add structured-output integration, rejection sampling, and tree branching only after the chain greedy path produces non-zero `accept_length`.

## EAGLE-3 specifics

(For the simple draft-model case, skip this section.)

EAGLE-3 is a one-layer drafter that conditions on target-model auxiliary hidden states. The target forward returns a small set of intermediate residual-stream states; the drafter fuses them, consumes the next token embedding, runs its own decoder layer, and emits draft logits. Its recurrent hidden output feeds the next draft step, so exact indexing matters.

**Shifted token/feature contract** (vLLM-compatible):

- Prompt prefill shifts inputs: drafter position `i` consumes `token[i+1]` paired with target aux features from position `i`.
- During decode, first choose the verifier/base token from target logits. The drafter then consumes that base token with the current target aux features and predicts the first draft token.
- Verification compares draft token `j` against target logits **after consuming** `base + draft[:j]`, not against the logits that produced `base`.
- Drafter KV must be advanced over accepted, rejected, and grammar-forced tokens with the same shifted token/feature pairs.

**Auxiliary hidden state details**:

- Layer IDs: `0 = embedding`, `1 = after target layer 0`, etc. vLLM default: `(2, num_layers // 2, num_layers - 3)`.
- Drafter computes its **logits** from the **final-normalized** hidden state.
- Drafter's **recurrent hidden** passed to the next EAGLE step is the **pre-final-norm** hidden.
- Mixing post-norm into the recurrent hidden corrupts every subsequent draft step.

**Vocab mapping (`d2t` / `t2d`)**:

- EAGLE-3 drafts into a sub-vocabulary of the target's. Drafter's argmax index is in **draft-vocab space**.
- To compare against the target: `target_token_id = draft_argmax + d2t[draft_argmax]`.
- Skipping this comparison gives 0 acceptance except by lucky low-index coincidences.

## Gating: when to use spec decode

Acceptance varies with workload. The wrong gate kills spec decoding even when it's working correctly.

**Don't**: gate on raw replay time (`verify_replay_ms < decode_replay_ms`) — the verify forward over `k+1` tokens *is* heavier per call; the win is per emitted token, not per call.

**Don't**: fall back permanently after one slow served request — the drafter and verify graphs need warm-up, and per-request acceptance is noisy at small N.

**Do**: measure **rolling-average effective tok/s** across N≥5 warm requests with all graphs captured, and compare against a verifier-only baseline measured on the same workload. Fall back only if the moving average trails baseline by some margin (3-5%) over the window.

**Do**: log per-request `attempted / accepted / verifier_steps / target_forwards / emitted_tokens` so the gate decision is auditable.

## Compatibility — engine pointers

| Engine | Core paths |
|:-------|:-----------|
| vLLM | `vllm/v1/spec_decode/{eagle,extract_hidden_states,metadata}.py`, `vllm/v1/sample/rejection_sampler.py`, `v1/cudagraph_dispatcher.py` |
| SGLang | `python/sglang/srt/speculative/{eagle_worker,eagle_worker_v2,multi_layer_eagle_worker,standalone_worker}.py`, `eagle_draft*cuda_graph_runner.py`, `adaptive_spec_params.py`, `adaptive_runtime_state.py` |
| TRT-LLM | `tensorrt_llm/_torch/speculative/{drafter,drafting_loops,eagle3,model_drafter,one_model_sampler,spec_tree_manager,draft_target}.py` |

## Pitfalls

- **Drafter without an incremental KV cache.** Calling `model(full_prefix)` per draft step costs O(prefix_len) every time and turns the drafter into the dominant request-time cost. The drafter needs the same paged-KV runner + captured one-token decode graph as the target.
- **Verify-extend bypassing CUDA graphs / FlashInfer batched wrappers.** A separate eager codepath (manual SDPA, hand-built attention mask) loses graph capture. Even at healthy acceptance, eager phases add enough launch overhead to net negative against verifier-only.
- **Branching the decode loop disables the non-spec graph too.** When spec decoding is on, the captured target decode graph for the non-spec fallback must keep warming and replaying. Profiles showing `cuda_graph_replays = 0` after wiring spec mean the fallback path also lost graph reuse — fix by routing the spec-on and spec-off paths through the same runner with explicit graph dispatch.
- **Bonus-position mask omitted on early grammar termination.** When the drafter's matcher fork hits a final state mid-draft, the post-loop "next-position" bitmask doesn't get appended. `verify_logits[:rows]` then leaves the bonus position unmasked and any token can be emitted — including a sentinel string in a numeric field. Always pad masks to `len(draft_ids) + 1` regardless of termination state.
- **KV rollback only resets `seq_len`.** Rejected draft tokens get K/V written into pages. Resetting only the sequence length leaves stale K/V visible to any code path that indexes via FlashInfer page metadata or recomputes from `key_positions[:new_seq_len]`. Roll back FlashInfer `paged_kv_indptr` / `paged_kv_last_page_len` and any other length tracker in lockstep.
- **Post-hoc schema-validation retry.** "Run the request, validate JSON, retry without spec on failure" is not a correctness mechanism — schema validity must come from per-token grammar masking + matcher advancement. Reviewers will reject it; it also burns a full retry on every malformed sample.
- **Drafter's `start_position`.** Position fed to the drafter's RoPE is the drafter's own position, not the target's. Off-by-position rotates the drafter's queries.
- **Quantization mismatch.** Drafts often run FP16 while target is FP8 — ensure verify comparison accounts for rounding, or quantize draft the same way.
- **CUDA graph with variable accept length.** The post-verify draft-extend (EAGLE / tree variants) has a variable shape. Capture per `max accept_length` bucket; pad up at runtime if accept is shorter.
- **Logit shape mismatch.** Target returns `(batch * (k+1), vocab)` logits flattened; sampler must unflatten correctly.
- **`accept_length.item()` on hot path.** Forces a CPU sync that defeats async scheduling. Keep it as a tensor; the draft-extend graph indexes it directly.

## See also

- [`backends/cuda-graph.md`](../backends/cuda-graph.md) — capture mechanics, fixed-shape and stable-address requirements
- [`backends/attention-backend-comparison.md`](../backends/attention-backend-comparison.md) — which attention backend to use for which phase under graph capture
- [`backends/flashinfer.md`](../backends/flashinfer.md) — FlashInfer batched wrappers (graph-safe) and the single-request wrappers (not graph-safe)
- [`algorithms/structured-output.md`](structured-output.md) — xgrammar interaction, jump-forward, grammar masking
