# AITER / CK engagement proof

Prove a kernel-library or tuning change actually reaches the live vLLM/SGLang
dispatch **before** trusting any measured delta from it. This is the ROCm
answer to "did the profile move because of my change, or because I measured
the wrong kernel" — see [`aiter.md`](aiter.md) for the library stack itself;
this file is about proving which piece of it actually ran.

## The one fact that decides everything

Only AITER's per-shape dispatch (and its config DB, when tuned) reaches the
live vLLM/SGLang GEMM and attention path. Offline tuning tools that sit
beside it do not automatically reach that path:

| Tier | Tool | Reaches the live serving dispatch? |
|:--|:--|:--|
| Library offline tuning | `hipblaslt-bench`, PyTorch `TunableOp` | Only if the live call site actually resolves to hipBLASLt *and* the tuning file is loaded there — many AITER-covered ops bypass this hook entirely. |
| **AITER dispatch (tuned or default)** | **AITER's per-shape config lookup** | **Yes — this is the live path for AITER-covered ops.** |

Tuning the wrong tier is the expensive failure mode: a real, measured win in
an isolated microbenchmark, and zero change on the server, because the tuned
result never gets consulted by the code path that actually runs.

## Engagement proof

```bash
AITER_LOG_TUNED_CONFIG=1 <launch server and drive real traffic>
grep -c 'is tuned on cu_num' server.log     # must be > 0
```

Zero hits means every lookup missed and any measured delta is noise, not a
tuning effect. The lookup resolves a multi-field key (GPU target first, then
CU count, shape, dtype, and flags such as bias/scale) — treat the exact field
list as version-dependent and verify it against your AITER version, but the
practical failure mode is stable across versions: **one mismatched field is a
100% silent miss**, with no error. The usual culprits are a `bias` flag tuned
one way and called the other, a dtype string mismatch, or a config captured
on a different GPU SKU or CU-partition mode than the one it's deployed on
(the CU count is part of the key, so a partition-mode change invalidates it).

## Cross-check with the trace, not just the log line

The log line proves the *lookup* succeeded; it doesn't prove the executed
kernel is the one you expect. Confirm independently with
`analyze_rocprof.py families` (see [`profiler.md`](profiler.md)) that the
dispatched kernel name belongs to the AITER/CK bucket, not a Triton or
torch-native fallback. A shape gap in AITER's coverage silently lands on
Triton — the log can stay quiet about this because "not found tuned config,
will use default" is a different code path than a lookup miss, and a default
(untuned) AITER dispatch and a Triton fallback can look similar in aggregate
GPU time without a kernel-name check.

## Re-tune when

Shapes, dtype, ROCm/AITER version, or the GPU SKU / CU-partition mode change.
The CU count is part of the lookup key, so a config tuned on one partition
mode will not match after a repartition even on the same physical GPU.

## Measured example: gfx90a coverage is narrower and must be checked, not assumed

This repo's MI210 bundle is the concrete case for "confirm which kernel ran
before concluding anything about relative hardware performance"
([`aiter.md`](aiter.md)):

- MI210 (gfx90a) has no native FP8, and AITER/CK coverage there is unverified
  relative to the gfx942 examples in [`aiter.md`](aiter.md).
- vLLM's own default attention backend (`ROCM_ATTN`) on this model/GPU
  measurably underperforms — the boot log shows its custom paged-attention
  kernel falling back to Triton for the chunked-prefill/decode path
  (`"Cannot use ROCm custom paged attention kernel, falling back to Triton
  implementation"`), and switching to `TRITON_ATTN` directly measured
  **+21–33% output tok/s** over accepting the silent fallback.
- PyTorch `TunableOp` (torch 2.9.1+rocm6.4) picks kernels from short timings
  on an otherwise-idle GPU. On this SKU, those picks **hold under sustained
  serving load only for decode-sized shapes**; picks for large prefill GEMM
  shapes lose to hipBLASLt's own defaults once the GPU is under load. A
  tuning run's picks are only as trustworthy as the load they were tuned
  under — verify under the real serving load, not just the tuning run's own
  acceptance gate.

Source: `examples/model-serving/qwen3.5-9b-mi210/config/platforms/mi210.toml`.

## Verify

| Check | How | Pass |
|:--|:--|:--|
| Engagement | `grep -c 'is tuned on cu_num' server.log` | **> 0** — check this first, always |
| Kernel identity | `analyze_rocprof.py families` | dispatched kernel is in the bucket you intended (AITER/CK), not a fallback |
| Real delta | same-session A/B | outside the noise band — [`measurement-protocol.md`](measurement-protocol.md) |
| Sanity | [`counter-triage.md`](counter-triage.md) | tuned config shows a roofline point closer to its roof, not just a shorter wall time |

## Failure modes

| Symptom | Cause | Fix |
|:--|:--|:--|
| Tuned config deployed, nothing changed | zero engagement | `grep 'is tuned on cu_num'`; check every key field, especially `bias` and dtype |
| Microbenchmark faster, server unchanged | tuned the wrong tier | only AITER's own dispatch reaches the live serving path for AITER-covered ops |
| `TunableOp` result ignored in production | the live call site bypasses that hook, or the picks don't hold under load | verify under real serving load, not the tuning run alone |
| Was engaged, now isn't | ROCm/AITER version bump, or SKU/CU-partition change | re-tune; the CU count is part of the lookup key |
| "AMD is slow" conclusion from a profile | never checked which kernel ran | `analyze_rocprof.py families` before concluding anything about the hardware |

## See also

- [`aiter.md`](aiter.md) — the AITER/CK/Triton/SDPA stack and when to pick each
- [`profiler.md`](profiler.md) — `analyze_rocprof.py families` and the capture recipes
- [`measurement-protocol.md`](measurement-protocol.md) — the A/B discipline engagement proof feeds into
