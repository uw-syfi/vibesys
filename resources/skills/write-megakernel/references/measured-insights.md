# Measured insights from the dense B200 experiment

This evidence supports scheduling hypotheses. It does not establish a universal
best tile size, a reachable SOL for every model, or performance on real weights.

## Workload and final result

The case `dense-step-qwen3-06b-b1-s128` executes 28 Qwen3-0.6B layers for batch 1,
with 128 prior KV positions and the current token (129 attended positions).
Hidden size is 1024, FFN size 3072, head dimension 128, query/KV head counts
16/8, and vocabulary size 151936. Weights and caches are BF16; dot products and
logits use FP32. The selected kernel uses SIMT warp-row matrix-vector products.

| Final comparison, same interleaved measurement | Warm GPU median |
|---|---:|
| Original Astra implementation | 4.540496 ms |
| Earlier optimized parent | 2.031488 ms |
| Selected implementation | **0.995248 ms** |

Forty measurements per implementation per fixture, across three fixtures,
produced selected medians of 0.982368, 0.997232, and 0.997792 ms. The combined
gain is 4.562 times versus original and 2.041 times versus parent. Compilation,
setup, and reference execution were excluded. This is a warm median on a B200
with CUDA compiler 13.1.115 and PyTorch 2.13.0+cu130, using Slurm.

A separate clean-rebuild audit measured GPU median 0.993008 ms and GPU p95
1.003726 ms; synchronized host median was 1.004336 ms. The result has a small
margin around 1 ms and establishes neither sub-1 ms host latency nor a tail
guarantee. Screening results near 0.928 ms were optimistic relative to final
validation and must not replace the final measurement.

## What the selected schedule does

| Technique | Mechanism and decision to transfer |
|---|---|
| Dependency-scoped synchronization | Per-KV-group projection readiness, per-attention-partition readiness, and per-head completion replace broad joins where consumers can advance independently. Whole-vector joins remain. |
| Split attention | Eight partitions per query head expose 128 attention CTAs instead of 16 head CTAs. Stable max/sum/value merging preserves softmax semantics. |
| Private normalization | Projection/MLP consumers compute RMSNorm in their own shared memory, avoiding a single normalization producer. Small duplicated reductions can be cheaper than producer latency and publication. |
| Fused Q/K normalization | Attention CTAs normalize and rotate the Q/K data they consume. Fixed public position 128 permits reusing RoPE coefficients across layers within an invocation. |
| Early weight transfer | Bulk async copies overlap the wait for activation producers. MLP weight transfers begin while O producers finish; down weights begin before gate activations are ready. |
| Shared activation reuse | O weights occupy 32 KiB of a 64 KiB buffer and O activations another 8 KiB. Down weights occupy 48 KiB and activations another 12 KiB. Disjoint live ranges remove repeated global activation loads. |
| LM-head pipeline and reduction | Double-buffer 16 KiB weight tiles. Each producer writes all its logits and tracks its stable partial argmax; the final reduction reads 296 partials. |
| Thin host wrapper | C++ allocates fresh output/workspace tensors, packs pointers, and launches on the current CUDA stream. Geometry/resource checks run at setup. |

The selected launch is **296 CTAs of 256 threads**, specialized for the measured
148-SM B200. It checks cooperative residency before launch. This geometry is an
experimental choice, not a default for another kernel or GPU. The final code
executes two grid barriers per call, initialization and the final LM-head join.

Layer transitions still include broad producer joins: 128 O producers,
**one counter for 192 MLP producers**, and 128 down producers. The MLP join also
ensures normalization readers finish before the residual is overwritten. A
stale source comment mentions four chunks, but the executed selected code uses
one. Removing grid barriers did not remove these mathematical/lifetime
dependencies.

## Paired screening evidence and rejected ideas

These are short screening comparisons within the indicated round. They expose
interactions and failed intuitions, not additive contributions to the final
4.562-times speedup. Parent timings also shifted between rounds, so compare
within a row rather than combining rows as a controlled trajectory.

| Record | Compared variants, median ms | Practical lesson |
|---|---|---|
| `round4.json` | parent 2.034; 128-thread, 3-CTA/SM variant 2.418; 4-CTA/SM 2.404 | More resident CTAs can add duplicated work and synchronization without increasing useful parallelism. |
| `round6.json` | parent 2.037; unroll2 4.441; unroll4 3.142; unroll8 2.874 | Inspect compiler resources and generated work before assuming more instruction-level parallelism helps. |
| `round9.json` | parent 2.040; private_norm 2.104 | Private normalization alone was insufficient in this schedule. |
| `round16.json` | bulk_all 1.642; early_mlp 1.549; early_mlp_local 1.417 | Earlier transfers and dependency changes helped together. |
| `round17.json` | early_mlp_local 1.417; private_norm 1.279; private_norm_chunks2 1.310 | Revisit a previously losing idea when the surrounding schedule materially changes. |
| `round20.json` | private_norm 1.279; finer flags 1.451; all flags 1.512 | Fine-grained readiness can lose to one completion counter when it offers insufficient overlap. |
| `round21.json` | private_parallel 1.243; pv16 1.212; argmax 1.209; combined 1.173 | Reduce both serial attention work and the final full-vocabulary scan. |
| `round26.json` | qknorm 1.042; LM pipeline 0.961; LM pipeline with larger grid 1.005 | Double buffering helped; the larger grid did not. |
| `round30.json` | best 0.959; cached O 0.933; cached down 0.950; both 0.929 | Reusing activations in spare shared capacity paid off in this geometry. |
| `round33.json` | Python dispatch 0.954; C++ dispatch 0.944 | Host preparation can matter near a tight GPU-event threshold. |
| `round34.json` | selected 0.942; larger LM tile 0.969 | A larger tile does not automatically amortize costs enough to win. |

Tensor-core projection variants also failed to improve their paired baselines
in rounds 18 and 19. That is evidence about those variants at batch 1, not a
claim that tensor cores cannot help other tilings, batches, or precision rules.

## What the profile does and does not establish

The final NCU capture reported replay duration 1.440992 ms, DRAM traffic rate
about 841 GB/s, eligible warps/scheduler about 0.167, barrier sample fraction
about 0.676, and long-scoreboard fraction about 0.128. These observations support
examining rendezvous, memory readiness, and insufficient ready work.

They do **not** establish that 67.6% of application time is spent at global
barriers, or that deleting barriers would recover that percentage. Samples
include local synchronization and dependencies; source-correlated analysis is
needed. The NCU replay duration is not the unprofiled latency result. A profile
of an earlier source version cannot establish the final version's bottleneck.

## Correctness and integrity evidence

Final validation passed ten fresh canonical trials, three timing fixtures, and
twenty alternating-input iterations with two calls each. Every required logit,
next token, and KV write was compared with the original tolerance. Inputs were
preserved. One GPU kernel was observed; memcheck, synccheck, and racecheck
reported no errors or hazards in their respective checks.

The later audit rebuilt only the unchanged source before inputs existed and
ran the candidate **before the oracle** in 89 checks. It perturbed all 16 input
fields at identical storage addresses, independently perturbed attention and
MLP weights in all 28 layers, checked cache endpoints and token/argmax ties,
interleaved models, retained old outputs, and exercised a nondefault stream.
Eighty perturbations changed the expected result enough to reject a stale
baseline. Source review found no input/answer cache, evaluator detection, CPU
model work, oracle access, or model work outside the timed invocation.

This audit covers the selected dense candidate with synthetic BF16 weights at
one fixed geometry. It is evidence of correctness and integrity, not a proof
for every possible input, checkpoint, architecture, or task.

## Transfer hypotheses for other MegaBench tasks

The following are **unmeasured transfer ideas** from this experiment:

- **MoE:** schedule actual routed expert tiles; account for routing, skew,
  uneven expert completion, and the join before weighted output combination.
  Avoid assuming balanced expert work or charging every expert's weights to a
  per-token bandwidth floor. Local readiness can reduce waiting for unrelated
  experts, but the final mixture still needs all selected contributions.
- **W8A16/W4A16:** stage packed weights and scale/zero metadata, overlap
  dequantization when independent work exists, and include unpacking cost in
  the roofline. Preserve the reference's group layout, rounding, and scaling.
- **Speculative verification:** exploit proposal-token parallelism when causal
  dependencies permit, then synchronize where acceptance and rollback depend
  on completed logits. Preserve every required KV/state update for full
  acceptance, partial acceptance, and rejection. Never optimize for the most
  common synthetic acceptance outcome.
- **Larger batches or longer context:** retune attention partitions and matrix
  tiles. Increased activation reuse may favor tensor cores and amortize shared
  normalization; the batch-1 SIMT schedule is not a prescribed solution.

## Evidence locations

These measurements came from the separate `vibe-megakernel` project. Paths
below identify its original experiment records; they are not included in the
VibeSys checkout. The source hashes identify the measured implementation, while
the CUDA teaching examples shipped with this skill can be built independently.
For campaign context, see [MegaBench issue 7](https://github.com/kamahori/vibe-megakernel/issues/7).

- Experiment root: `megabench/experiments/2026-10-06/20-40-09-astra-dense-1ms/`.
  Read `final-summary.json` and `results/final-cpp.json` for the final result;
  `results/round*.json` are screening records. `summary.json` is superseded.
- Selected source: `bulk_cpp_launch/submission.py` and
  `bulk_cpp_launch/candidate_helpers/decode.cu` under that root. Other folders,
  including `best/`, represent earlier candidates.
- Final profile: `profiles/bulk_cpp_launch/capture.ncu-rep` and
  `profiles/bulk_cpp_launch/source-summary.json` under that root.
- Independent audit:
  `megabench/experiments/2026-10-06/22-39-37-dense-integrity-audit/audit-summary.json`.
- Task contract: `megabench/docs/AGENT_TASK.md`; original checker and measurement
  implementation: `megabench/harness/correctness.py` and `benchmark.py`.

Selected source SHA-256:

```text
submission.py:
eda84522067cfcee98a24ebddcd6e91b1742796b10918fba16c99b9b7a6366ac
candidate_helpers/decode.cu:
34a16621c38751efb70102ef6cd1afd81015d748323f1cff76d1fcd6a92dd4b2
```
