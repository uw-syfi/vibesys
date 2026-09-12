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
- `--speculative-draft-model-path <original, unsharded checkpoint>`
- `--speculative-draft-load-format auto`

See "Pitfalls" below for why the last two flags are mandatory rather than optional on this checkpoint's load path.

This recipe runs with mixed chunked prefill effectively off: the engine forces `enable_mixed_chunk` off whenever a speculative algorithm is set, regardless of whether `--enable-mixed-chunk` is in argv. See [`engines/sglang.md`](../../engines/sglang.md) for the mechanism.

The aiter attention backend (already the platform default, see [`aiter.md`](aiter.md)) separately gives the draft its own `AiterMultiStepDraftBackend`, keyed off the same `--attention-backend`; no extra flag needed for that either.

k=3 was chosen over k=2 by the lower-median-TPOT rule after both cleared gates and stayed within a 10 percent p95-TTFT budget: a probe measured k=2 at -45.1 percent TPOT with p95 TTFT -8.6 percent, and k=3 at -48.8 percent TPOT with p95 TTFT +2.2 percent.

## Results

| Concurrency | TPOT (median) | pooled p95 TTFT turn2+ | accept length (median, of 4) |
|:--|--:|--:|--:|
| Uncapped, 48 sessions | 70.86 -> 38.27 ms (-46.0%) | 815.6 -> 759.0 ms (-6.9%) | 2.89 |
| 16-session cap | 22.10 -> 13.49 ms (-39.0%) | 423.6 -> 451.8 ms (+6.7%) | 2.84 |

Gates 13/13 on every rep at both concurrencies. Boot cost: 2.6 to 2.8x longer than the non-speculative boot (about 800 to 900 s versus about 300 s), because the draft head loads from the unsharded checkpoint and boot captures extra decode graphs for the draft path; a deployment-time cost only, not a serving-time one.

Status: verified. Stamp: sglang-v0.5.18-rocm700-mi30x, benchmark_version 4, 2026-09-12, jobs 633511 (uncapped), 633512 (16-session cap).

## Candidate: disable the overlap scheduler for this recipe

A probe at 48 sessions (3 reps per side) against the accepted NEXTN k=3 configuration found `--disable-overlap-schedule` cuts pooled p95 TTFT turn-2+ by 31 percent (749 to 517 ms) and p50 by about one scheduler iteration (97 ms), at +5.7 percent mean TPOT. See [`../../engines/sglang.md`](../../engines/sglang.md)'s overlap-scheduler pitfall for the mechanism (a long-step, TTFT-bound multi-turn workload pays the overlap scheduler's one-iteration publish lag on every first token). Not yet part of the validated recipe above.

Status: candidate, acceptance pending (5-rep, two concurrencies). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job 633552.

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
Fix:     set `--speculative-draft-model-path <original checkpoint>` and
         `--speculative-draft-load-format auto` explicitly. Both are
         mandatory on this checkpoint and this engine; neither has a
         working default here.
Scope:   rocm, this checkpoint's TP-sharded fast-path artifact. The
         missing auto-default entry is an engine-wide allowlist gap, not
         rocm-specific, but is recorded here because the sharded-artifact
         mechanics that make it bite are this fork's ROCm load path.
Status:  verified. sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job 633510.
```

## See also

- [`floor.md`](floor.md): the base TP=4 launch recipe this recipe extends
- [`weight-loading.md`](weight-loading.md): sharded-artifact mechanics, and the expert-parallel load-path pitfall blocking an EP-based draft/verify layout
- [`../../algorithms/speculative-decoding.md`](../../algorithms/speculative-decoding.md): the portable contract
