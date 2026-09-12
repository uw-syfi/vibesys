# Speculative decoding (NEXTN/MTP) on ROCm

Backend `rocm` recipe for the portable contract in [`../../algorithms/speculative-decoding.md`](../../algorithms/speculative-decoding.md).

## Validated recipe: NEXTN with the checkpoint's own MTP head

Qwen3.5-397B-A17B-MXFP4 ships a co-trained MTP draft head (`mtp.fc.weight`, a full 512-expert MoE at `mtp.layers.0`, `mtp_num_hidden_layers` 1): no separate draft model or extra training is needed. Accepted configuration: NEXTN, k=3 draft steps (4 verify tokens), `eagle-topk` 1, greedy verify (temperature 0, exact match to non-speculative decode per the portable contract's invariant 3), plus the linear-replay SSM fast path for the hybrid Gated-DeltaNet layers.

argv, on top of the accepted TP=4 launch recipe in [`floor.md`](floor.md):

- `--speculative-algorithm NEXTN`
- `--speculative-eagle-topk 1`
- `--speculative-num-steps 3`
- `--speculative-num-draft-tokens 4`: num_draft_tokens = num_steps + 1 for a linear (topk=1) chain.
- `--enable-linear-replayssm-spec`: replaces per-draft full mamba-state snapshots with a per-slot raw-input window; gated to a linear draft chain only (`speculative_eagle_topk` in `{None, 1}`), which NEXTN satisfies. Requires the linear-attention decode backend to be `triton` or `flashinfer`; this platform's default (`triton`) already satisfies it, no extra flag needed.
- `--speculative-draft-model-path <draft-only sharded artifact, or the original unsharded checkpoint>`
- `--speculative-draft-load-format sharded_state` (with the draft-only artifact) or `auto` (with the original checkpoint)

See "Pitfalls" below for why the last two flags are mandatory rather than optional on this checkpoint's load path, and for the draft-only sharded artifact that makes the fast path possible.

This recipe runs with mixed chunked prefill effectively off: the engine forces `enable_mixed_chunk` off whenever a speculative algorithm is set, regardless of whether `--enable-mixed-chunk` is in argv. See [`engines/sglang.md`](../../engines/sglang.md) for the mechanism.

The aiter attention backend (already the platform default, see [`aiter.md`](aiter.md)) separately gives the draft its own `AiterMultiStepDraftBackend`, keyed off the same `--attention-backend`; no extra flag needed for that either.

k=3 was chosen over k=2 by the lower-median-TPOT rule after both cleared gates and stayed within a 10 percent p95-TTFT budget: a probe measured k=2 at -45.1 percent TPOT with p95 TTFT -8.6 percent, and k=3 at -48.8 percent TPOT with p95 TTFT +2.2 percent.

## Results

| Concurrency | TPOT (median) | pooled p95 TTFT turn2+ | accept length (median, of 4) |
|:--|--:|--:|--:|
| Uncapped, 48 sessions | 70.86 -> 38.27 ms (-46.0%) | 815.6 -> 759.0 ms (-6.9%) | 2.89 |
| 16-session cap | 22.10 -> 13.49 ms (-39.0%) | 423.6 -> 451.8 ms (+6.7%) | 2.84 |

Gates 13/13 on every rep at both concurrencies. Boot cost without the draft-only sharded artifact: 2.6 to 2.8x longer than the non-speculative boot (about 800 to 900 s versus about 300 s), because the draft head loads from the unsharded checkpoint and boot captures extra decode graphs for the draft path. With the draft-only sharded artifact (see [`weight-loading.md`](weight-loading.md)), boot drops to about 365 s. Either way this is a deployment-time cost only, not a serving-time one: TPOT, p95 TTFT, and accept_len are unaffected by which draft load path was used.

Status: verified. Stamp: sglang-v0.5.18-rocm700-mi30x, benchmark_version 4, 2026-09-12, jobs 633511 (uncapped), 633512 (16-session cap).

## Accepted: disable the overlap scheduler for TTFT-weighted multi-turn workloads

On top of the NEXTN k=3 recipe above, `--disable-overlap-schedule` cuts pooled p95 TTFT turn-2+ by 24.1 percent at 48 sessions uncapped (734.4 to 557.5 ms) and by 28.4 percent at a 16-session cap (438.5 to 313.8 ms), at a median TPOT cost of +3.3 percent (37.21 to 38.45 ms) and +6.8 percent (13.17 to 14.06 ms) respectively. accept_len is unchanged (2.85 vs 2.86 of 4 at 48 sessions, 2.84 vs 2.82 at the 16-session cap).

See [`../../engines/sglang.md`](../../engines/sglang.md)'s overlap-scheduler pitfall for the mechanism: with the overlap scheduler on, the scheduler commits the next batch before the current step's results return, so a request that arrives mid-step waits an extra iteration (about 110 ms with spec decode) before its first token is admitted and published; with it off, admission sees a fresh poll every iteration, at the cost of putting the scheduler's own per-step CPU work back on the device's critical path (the TPOT regression).

Rule: for TTFT-weighted multi-turn workloads with spec decode, turn the overlap scheduler off; for throughput-weighted workloads, keep it on.

- `--disable-overlap-schedule`: add to the argv above when the deployment is TTFT-weighted.

Status: accepted. Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12, jobs 633754 (48 sessions uncapped) and 633755 (16-session cap), 5 reps per side each, gates 13/13 every rep.

## Interaction: PyTorch TunableOp tuned dense GEMM

Each draft step (`draft_decode`) reruns the same dense projections and the LM head as the verify step, at its own M (batch size, not batch size x draft tokens). A TunableOp table covering the CUDA-graph capture set's M values (see [`floor.md`](floor.md) and [`aiter-tunableop.md`](aiter-tunableop.md)) therefore speeds up all `k` draft steps as well as the verify step, not just the one call a naive estimate would count: this multiplication is most of why the measured end-to-end TPOT gain from tuning comes out well above a verify-only prediction.

## Pitfalls

### The draft head has no sharded fast-path artifact; point the draft at the original checkpoint

```
Symptom: server boots with `--speculative-draft-model-path` pointed at
         the same TP-sharded artifact the target loads from, and either
         fails to find the MTP weights, or (if the flag is omitted
         entirely) never engages the draft path at all, because this
         checkpoint's architecture is not on the engine's fixed arch
         allowlist that auto-defaults `--speculative-draft-model-path`
         to `--model-path`.
Cause:   the TP-sharded fast-path artifact this checkpoint normally boots
         from contains zero `mtp.*` tensors, and its own
         `model.safetensors.index.json` is a stale copy of the original
         checkpoint's index (it lists file names the sharded artifact
         does not contain), so it cannot be used to locate the head
         either. The MTP head exists only in the original, unsharded
         checkpoint. Separately, the draft model's own load format
         silently inherits the target's `--load-format` unless
         overridden; the target here boots with `sharded_state`, a
         layout the original checkpoint is not in.
Fix:     fast path: save a draft-only `sharded_state` artifact (see
         [`weight-loading.md`](weight-loading.md)'s sharded draft
         artifact recipe) and set `--speculative-draft-model-path
         <draft-only sharded artifact>` with
         `--speculative-draft-load-format sharded_state`; this cuts the
         draft's own load-weight phase from about 498 s to about 9.7 s.
         Where no such artifact exists yet, fall back to
         `--speculative-draft-model-path <original checkpoint>` and
         `--speculative-draft-load-format auto`. One of these two is
         mandatory; neither has a working default here.
Scope:   rocm, this checkpoint's TP-sharded fast-path artifact. The
         missing auto-default entry is an engine-wide allowlist gap, not
         rocm-specific, but is recorded here because the sharded-artifact
         mechanics that make it bite are this fork's ROCm load path.
Status:  verified. sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job 633510
         (original-checkpoint fallback); fast path verified in jobs
         633762/633763 (draft-only sharded artifact).
```

## See also

- [`floor.md`](floor.md): the base TP=4 launch recipe this recipe extends
- [`weight-loading.md`](weight-loading.md): sharded-artifact mechanics, and the expert-parallel load-path pitfall blocking an EP-based draft/verify layout
- [`../../algorithms/speculative-decoding.md`](../../algorithms/speculative-decoding.md): the portable contract
