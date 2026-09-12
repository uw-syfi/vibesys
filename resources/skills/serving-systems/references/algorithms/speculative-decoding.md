# Speculative decoding — contract

Propose `k` candidate tokens cheaply, verify them in **one** target forward, keep the longest accepted prefix. Trades slightly more work per call for fewer target calls.

The verify algorithm is portable. The execution strategy around it is not — variable accepted length interacts with each backend's shape model differently — so implementations live under `platforms/`.

## Drafter variants

Portable across backends; pick on quality/cost grounds, not hardware.

| Variant | Drafter | Notes |
|:--|:--|:--|
| **Draft-model** | separate small LM | simplest; own KV cache and warmup |
| **MTP** | multi-token-prediction heads on the target | one forward yields target + draft logits |
| **Medusa** | multiple LM heads | heads predict different relative positions |
| **EAGLE-3** | one-layer model on target hidden states | best acceptance per cost; needs `d2t`/`t2d` vocab projection |
| **n-gram / suffix** | context lookup | cheap, low acceptance, good on repetitive prompts |

## Invariants

1. **One target forward per verify.** Build the verifier input as `prompt_kv + [base, d_1 … d_k]` and run *one* forward over `k+1` query positions. Doing `k+1` separate forwards gains nothing — this is the entire optimization.
2. **Drafter decodes incrementally.** The drafter needs its own KV cache and prompt prefill, advancing only over emitted tokens. Re-running a full forward over the growing context per draft step makes the drafter cost more than the target it was meant to save.
3. **Accept prefix, then stop.** Halt at the first rejection; position `k` yields a free bonus token. The accept test depends on the sampling mode:
   - **Greedy** (`temperature == 0`): accept while `argmax(target_logits[i]) == d_{i+1}`.
   - **Stochastic** (`temperature > 0`): accept with probability `min(1, p/q)` where `p` is the target's and `q` the drafter's probability for the drafted token; on rejection, resample from the normalized residual `max(0, p - q)`. Using the greedy rule here **silently biases the output distribution** — the result is no longer equivalent to sampling from the target model.
4. **Roll back all length trackers together.** Rejected drafts already wrote K/V. Resetting only `seq_len` leaves stale K/V visible to any path that indexes by cache metadata.
5. **Verify uses the same execution path as normal decode.** A separate eager verify path costs more than it saves and can disable the fast path for non-speculative requests too.
6. **Load drafter weights through the same fast path as the target.** A drafter or MTP head is small next to the target, but a slow, unthreaded, per-tensor loader still costs real boot time if the drafter falls back to it while the target uses a sharded or threaded fast path; give the drafter the same fast loading path.

## Gating

Acceptance is workload-dependent, and the wrong gate kills a working implementation:

- **Don't** gate on per-call time — the verify forward *is* heavier per call; the win is per emitted token.
- **Don't** fall back permanently after one slow request — warmup and per-request acceptance are noisy.
- **Do** compare rolling-average effective tok/s over N≥5 warm requests against a verifier-only baseline on the same workload.
- **Do** log `attempted / accepted / verifier_steps / target_forwards / emitted_tokens` so the decision is auditable.

## Choosing k (draft length)

Expected accepted length under a per-slot acceptance rate `a` is a geometric sum: `sum_{i=0}^{k} a^i`. Each additional draft position adds `a^(k+1)` tokens of expected value, a strictly smaller increment than the one before it, while cost keeps growing: one more draft forward per round, plus a larger verify batch (`M = N x (k+1)` for N sequences). Raising k therefore has a break-even point; past it, the added draft-forward and verify-batch cost outweighs the value of the extra accepted tokens. Where that point falls depends on `a`, so a workload with a higher per-slot acceptance rate can push the break-even further out than one with a lower rate.

Track both numbers when tuning k, not just one:

- **Expected accepted length** (`accept_len`): the value that determines token savings per verify call.
- **Per-slot acceptance rate** (`a`): the value that predicts whether the *next* increment of k is still worth it.

`accept_len` can rise while `a` falls: adding a chain position increases the geometric sum even when the newly added term is individually less reliable than the terms before it. A rising `accept_len` alone does not show the per-token rate held up, and does not by itself justify a larger k. Check the marginal cost (draft forward plus verify batch growth) against the marginal `a^(k+1)` gain, not just the trend in `accept_len`.

See [`platforms/`](../platforms/) for measured k-choice numbers per backend.

## Failure modes if skipped

| Symptom | Usually means |
|:--|:--|
| Throughput below verifier-only baseline | invariant 1 or 2 — separate forwards, or a non-incremental drafter |
| Corrupt output after a rejected draft | invariant 4 — partial rollback |
| Non-speculative requests also got slower | invariant 5 — branching disabled the shared fast path |
| Acceptance near zero | drafter/target vocab or position mismatch, not a perf problem |
| Boot time dominated by the drafter's own weight load | invariant 6, drafter using a slow/generic loader instead of the target's fast path |

## Engine interactions

SGLang forces `enable_mixed_chunk` off whenever a speculative algorithm is set (an assertion at boot, not a runtime choice), so prefill and decode never share an iteration under speculative decoding on that engine; see [`engines/sglang.md`](../engines/sglang.md).

## Platform implementations

The divergence is **variable accepted length**, which is a shape change per step:

| Backend | Strategy |
|:--|:--|
| `cuda` | Capture per `(batch bucket, k)` shape; pad up when accept is shorter |
| `rocm` | As cuda. Validated end to end (NEXTN/MTP) for a checkpoint shipping its own co-trained draft head; see [`platforms/`](../platforms/) for the recipe, k-choice, and the checkpoint's draft-model load-path pitfalls |
| `trainium` | Keep accepted length out of the graph shape entirely — verify at fixed width `k+1` and commit by masking. NxD's supported path is **fused speculation** (`fused_speculation`), which compiles drafter and target together and handles variable accept length internally; it is also a prerequisite for `async_mode` |
| `metal` | No capture step; the variable shape is not a problem, and the cost model differs — evaluate whether spec decoding pays at all before building it |

## See also

- [`algorithms/batched-sampling.md`](batched-sampling.md) — rejection-sample verify shares this machinery
- [`algorithms/structured-output.md`](structured-output.md) — grammar masking interaction; drafts outside the grammar reduce acceptance
- [`platforms/`](../platforms/) — the implementation for the selected backend
