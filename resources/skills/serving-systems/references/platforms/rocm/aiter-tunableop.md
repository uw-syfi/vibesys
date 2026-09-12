# PyTorch TunableOp tuned dense GEMM (gfx942 detail)

Full recipe, mechanism, and pitfall behind the summary in [`aiter.md`](aiter.md#pytorch-tunableop-accepted-for-the-dense-projections-and-lm-head). The filename-substitution rule and the unconditional at-exit rewrite are PyTorch-internal and documented once, portably, in [`../../frameworks/pytorch.md`](../../frameworks/pytorch.md); this file covers the gfx942/Qwen3.5 tuning coverage, recipe, and the measured serving-level effect.

## Tuning coverage

A pure-torch TunableOp sweep over every M value the CUDA-graph capture set dispatches (36 M values across the target-verify, draft-decode, draft-extend, and gate-probe capture sets) times the model's six per-rank dense-projection shapes plus the LM head (`F.linear`/`torch.matmul` share one TunableOp cache key, so tuning one covers both) tunes all 252 cells, 0 misses, 0 errors. Per-forward dense GEMM plus LM head time drops from about 23 ms to 2.6-6.2 ms across the sampled M values (1.32x to 18.65x per cell on GPU kernel time); max abs diff 9.77e-4 against the untuned baseline (one bf16 ULP).

This is a different tuning path from the aiter tuned-GEMM gap in [`aiter.md`](aiter.md): TunableOp tunes at the `F.linear`/`torch.matmul` dispatch level and does not depend on aiter's own `(gfx, cu_num)`-keyed lookup table, so it recovers that gap without an aiter-side config regeneration.

Status: verified. Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-12, job 633793.

## Recipe

1. Tune in a pure-torch process (no engine), with `PYTORCH_TUNABLEOP_TUNING=1`, over exactly the M values the CUDA-graph capture set dispatches for this deployment: verify batch size x draft tokens, draft-decode batch size, draft-extend batch size x draft tokens, plus the gate-probe values. Confirm the capture batch-size list from the server log before tuning rather than assuming it.
2. Let the sweep run to completion; each new shape is benchmarked against every candidate algorithm and the winner appended to a results CSV.
3. Copy the resulting per-rank CSVs (one file per device ordinal) into the serving image and make them read-only (see the pitfall below for why per-rank files, not one shared path).
4. Serve with `PYTORCH_TUNABLEOP_ENABLED=1`, `PYTORCH_TUNABLEOP_TUNING=0` (replay only, tune nothing new), `PYTORCH_TUNABLEOP_FILENAME` pointed at the per-rank files (a literal `%d` placeholder, or rely on PyTorch's own before-the-extension insertion; see [`../../frameworks/pytorch.md`](../../frameworks/pytorch.md)).
5. Gate the launch on every rank logging a successful table load with no could-not-open line (see the pitfall below).
6. The results-file validator header pins the exact PyTorch, ROCm, hipBLASLt, and GPU (gfx target) versions the table was tuned under, and is silently ignored on a mismatch (falls back to untuned, no error). Regenerate the table on any change to that stack.

## Per-device filename substitution voided the first acceptance attempt

```
Symptom: loading this table via PYTORCH_TUNABLEOP_ENABLED=1
         PYTORCH_TUNABLEOP_TUNING=0 PYTORCH_TUNABLEOP_FILENAME=<one path>
         on a TP=4 server only speeds up rank 0; the other ranks log a
         could-not-open error or run untuned from an empty file, and the
         TP step waits for the slowest rank, so p95 latency gets worse,
         not better, than the untuned baseline.
Cause:   PyTorch substitutes the device ordinal into the filename; see
         [`../../frameworks/pytorch.md`](../../frameworks/pytorch.md) for
         the substitution rule and the unconditional at-exit rewrite,
         which apply here unchanged. The first acceptance attempt shipped
         one single-device-tuned CSV named for rank 0 only; ranks 1-3
         either logged could-not-open, or, on the second job, loaded an
         emptied file that the first job's own at-exit rewrite had
         produced for that same path.
Fix:     write one file per rank (<name>0.<ext> through <name>3.<ext>) up
         front, make them read-only, and gate the launch on every rank
         logging a successful load with no could-not-open line.
Scope:   rocm, gfx942, sglang TP=4, this image's torch 2.9.0a0.
Status:  verified (mechanism per pytorch.md; reproduced here with the
         wrong file layout, then fixed and reproduced correct with the
         per-rank layout). sglang-v0.5.18-rocm700-mi30x, 2026-09-12,
         jobs 633800, 633801 (void); 633839, 633841 (fixed, accepted
         below).
```

## Accepted result

Paired against the NEXTN k=3 + `--disable-overlap-schedule` base configuration (identical flags, TunableOp off vs on), 5 reps per side, gates 13/13 every rep (20 reps total):

| Concurrency | TPOT (median) | pooled p95 TTFT turn2+ | accept_len (median) |
|:--|--:|--:|--:|
| Uncapped, 48 sessions | 38.05 -> 22.86 ms (-39.9%) | 499.0 -> 493.3 ms (-1.1%, within rep spread) | 2.845 vs 2.871 |
| 16-session cap | 14.31 -> 12.44 ms (-13.1%) | 331.5 -> 334.4 ms (+0.9%, within rep spread) | 2.841 vs 2.848 |

accept_len is statistically unchanged at both concurrencies, confirming TunableOp changes only GEMM kernel dispatch, not the speculative accept/reject logic.

Status: accepted. Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-12, jobs 633839 (48 sessions), 633841 (16-session cap).

## Mechanism: why the TPOT gain exceeds the single-verify prediction

A verify-only estimate from the tuning coverage above (the `target_verify` dense+LM-head saving at M=192, 17.4 ms of a ~110 ms round) predicts about 16-17 percent TPOT. NEXTN k=3 runs three more dense+LM-head forwards per round, one per draft step (`draft_decode`, M=48 at 48 sessions), each also an exact tuned-table hit (19.6 ms combined saving per call). Counting all four forwards gives an upper bound of 17.4 + 3(19.6) = 76.3 ms/round; the measured saving, back-computed from the TPOT drop and the unchanged accept length (`delta_TPOT x accept_len`), is about 43.4 ms/round, between the single-verify estimate and the 4-forward ceiling. This split is inferred from the aggregate per-M tuning table, not measured directly by a per-forward-type trace in the live server.

## Mechanism: why p95 TTFT is unchanged

`target_verify`, `draft_decode`, and `draft_extend` are decode-side/verify-side CUDA-graph capture sets; prefill capture is disabled for this deployment, so prefill runs eagerly at whatever token count each request's prompt produces, essentially never one of the 36 tuned M values. TunableOp's cache key is the exact (M, N, K), so a prefill GEMM misses the tuned table on both sides of the comparison and falls back to the same untuned kernel either way. p95 TTFT is dominated by turns that land behind a prefill batch, so its wait time is governed by this untouched path on both sides.

## Boot cost

CUDA-graph capture with TunableOp enabled adds a one-time per-shape hipBLASLt handle-validation cost the untuned path does not pay (it always dispatches the same fixed tile regardless of M, so there is nothing new to validate per shape). Observed range: +100 s at 48 sessions in one run, roughly net neutral at a 16-session cap in another, with the `target_verify` capture phase alone still showing a comparable one-time cost in both. Boot-time deltas are node- and run-dependent noise on top of a real per-shape capture cost, not a fixed tax; not part of the acceptance criteria.

## Prefill M values: measured, not pursued

The recipe above tunes the fixed decode/verify/draft M values a CUDA-graph capture set dispatches. Prefill M is different: prefill runs eagerly (capture is disabled for this deployment), so its M is whatever token count each request's batch produces, arbitrary and effectively never repeated exactly. TunableOp's cache key is the exact (M, N, K); an arbitrary prefill M almost never hits a tuned cell, so using TunableOp at prefill M at all requires a bucketing or padding scheme (round M up to the nearest tuned bucket, pad the call, tune one table entry per bucket) rather than tuning-as-is.

A dedicated sweep (12 M values from 256 to 2048, including this campaign's own 337 and 919 baseline/p95 extend lengths, all seven per-rank dense-projection shapes) measured what such a scheme would cost and save:

- Per-forward dense GEMM (345 launches + LM head): M=337 26.1 -> 7.7 ms, M=919 28.8 -> 15.3 ms, M=2048 46.1 -> 28.4 ms. Saving shrinks from about 75 percent at M=256 to 31-47 percent at M=919-1536 as the untuned default tile gets relatively less oversized at higher M.
- Padding tax, isolated (tuned M=384 serving an M=337 request vs. tuned M=337 directly): +0.123 ms, about +1.6 percent, small once tuning is applied at all.
- Bucket-table cost is real: only the single largest-launch-count shape (`moe_gate_shared_up`) is bucket-friendly (one tile covers 5 of 6 M in 256-448); the other six shapes change their winning tile at nearly every M in the same range (3-5 distinct tiles across 6 M values each), so a bucketing rollout needs close to one table entry per bucket for most shapes, not a handful of coarse buckets.
- Predicted effect on pooled p95 TTFT: about -2.4 to -3.2 percent (two independent estimation methods), because dense GEMM is only 13-18 percent of the prefill window at these token counts and p95 turns are dominated by MoE-routed compute and queueing, neither of which dense-GEMM tuning touches. Below the 5 percent bar this campaign uses for a standalone engine PR; not pursued as one. Predicted p50 TTFT effect is larger (-7.4 to -9.8 percent) but the acceptance bar is on p95.

### Pitfall: a tuned cell can be slower than the untuned default at some M

```
Symptom: at M=1536, two of seven tuned dense-projection shapes
         (gdn_in_proj_qkvz, attn_qkv_proj) measure slower tuned than
         untuned (0.88x, 0.89x); a third (lm_head) is 0.93x. Confirmed on
         both the GPU-kernel trace and the host wall-clock, not a
         single-metric artifact.
Cause:   TunableOp's tuning search (--max-tuning-iterations 30, a few
         warm calls per M) can commit to a candidate that measured
         (noisily) faster than the untuned default during tuning but is
         not actually faster on replay; TunableOp does not compare
         against the untuned default as one of its own candidates, so
         nothing catches this at tune time.
Fix:     do not ship a tuned table (bucketed or not) as-is at every M
         without a re-measurement or ensembling pass that compares each
         tuned cell against the untuned default before committing it;
         verify per M, not just at the M values a spot-check happens to
         cover.
Scope:   PyTorch TunableOp, any (gfx, cu_num) combination; observed
         values are gfx942, this image's stack.
Status:  verified (measured, cross-validated on trace and wall-clock).
         sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job 633873/634017.
```

Status: verified (measured on a real one-device sweep; not reproduced a second time, but the mechanism, an exact-(M,N,K) cache key against arbitrary prefill M, is read from PyTorch's own TunableOp dispatch, see [`../../frameworks/pytorch.md`](../../frameworks/pytorch.md)). Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-12, jobs 633873 (tuning + coverage, TIMEOUT on the trace step after 45/84 cells) and 634017 (completion job, remaining 39 trace cells against the same tuned CSV).

## See also

- [`aiter.md`](aiter.md): the aiter tuned-GEMM gap this recovers, and the accepted-summary entry that links here
- [`floor.md`](floor.md): the validated launch recipe's TunableOp environment block
- [`../../frameworks/pytorch.md`](../../frameworks/pytorch.md): the portable TunableOp contract and filename-substitution rule
- [`speculative-decoding.md`](speculative-decoding.md): the NEXTN k=3 + overlap-off base configuration this stacks on
