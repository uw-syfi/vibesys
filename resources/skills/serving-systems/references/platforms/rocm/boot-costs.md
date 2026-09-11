# Boot and warmup costs (sglang fork, gfx942)

Scope: backend `rocm`, gfx942 tested, sglang fork. Status: verified unless marked. Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-11.

Purpose: one-time, process-lifetime (or per-bucket) costs on this stack that belong in warmup accounting, not steady-state measurement, and the pitfall that makes one of them recur on every boot instead of once.

## Gated DeltaNet prefill kernel: one autotune sweep, plus one recompile per NT_BUCKET

Qwen3.5's Gated DeltaNet (linear-attention) prefill path (`chunk_gated_delta_rule_fwd_kernel_h_blockdim64` and its accompanying kernels) pays two distinct one-time Triton costs per process:

- About 14.0 s on the very first prefill call of the process's life, dominated by a 6-config autotune sweep of `chunk_gated_delta_rule_fwd_kkt_solve_kernel` (7.10 s of the 14.0 s alone). This kernel's autotune key has no token-count dependence, so it fires exactly once per process regardless of shape.
- About 0.92 s the first time a batch's total new-token count crosses one of three `NT_BUCKET` boundaries (`NT_BUCKET = 0 if NT<=32 else (1 if NT<=128 else 2)`, `chunk_delta_h.py`), because that kernel's autotune key does include the bucket. At most 2 such recompiles occur in a process's life; production token counts on this workload mostly stay in bucket 0. In production server logs, the single NT_BUCKET-crossing event observed in a whole server's life produced a print-to-print gap of 1 s, matching the measured 0.92 s cost closely.

Every fresh token count that does not cross a bucket boundary compiles in under 1.5 ms; the cost is bucket-keyed, not token-count-keyed, so it cannot explain a stall recurring at repeated, already-warm token counts.

Scope: rocm, gfx942, sglang fork's Gated DeltaNet / linear-attention prefill path. Status: verified (mechanism read from source at `chunk_fwd.py:30-36` and `chunk_delta_h.py:29-51,383`, reproduced on-device). Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-11, job 632946.

## Custom HIP extensions: about 50-55 s cold build per extension

The fork's two custom HIP extensions (`sglang_mxfp4_fused_moe`, gated by `SGLANG_MXFP4_MOE_HIP`; `sglang_skinny_gemm_bf16`, gated by `SGLANG_SKINNY_GEMM`; see [`aiter.md`](aiter.md)) each cost about 50 to 55 s to build cold via `torch.utils.cpp_extension.load`, independent of the M shape that triggers the first call. Once built, every subsequent call at any M pays no additional compile cost (measured: M=1023 first call 11.87 ms, second call 11.96 ms, within noise).

This build is separate from AITER's own JIT cache (see [`aiter.md`](aiter.md), JIT cache section), which has no per-launch rebuild problem.

Scope: rocm, gfx942, sglang fork. Status: verified (measured on-device, first vs second call at fresh M). Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-11, job 632946.

## The HIP extension loader keys staleness on path and mtime, not content

```
Symptom: both custom HIP extensions above rebuild from source on every
         fresh boot (~45-60 s combined), even when a persistent, shared
         build-cache directory is passed to the extension loader, and
         even though the source has not changed; boot time roughly
         doubles (539 s measured vs a 287 s baseline with an effective
         cache).
Cause:   each launch stages the checkout into a freshly named directory
         (e.g. a new tmpfs path per launch), so the two extensions'
         source files' absolute path changes on every relaunch.
         torch.utils.cpp_extension.load()'s staleness check is keyed on
         source path and mtime, not content, so it cannot recognize a
         previously built artifact at a new source path as still valid,
         and rebuilds unconditionally even though the persistent cache
         directory it is pointed at already holds a matching build.
Fix:     content-hash the build directory and copy sources into it at a
         fixed mtime before calling cpp_extension.load, so the staleness
         check sees the same path and mtime across launches whenever the
         content is unchanged. Measured: first boot after the fix (cold
         build into the hashed directory) 449 s; second boot (cache hit,
         no hipcc invoked) 263 s; build artifacts byte-identical between
         the two boots.
Scope:   rocm, sglang fork's HIP extension loader; the underlying
         mechanism (torch.utils.cpp_extension.load keys staleness on
         source path and mtime, not content) is site-independent and
         applies to any cpp_extension-based build triggered from a
         per-launch staging path, not only this fork.
Status:  verified (measured, two boots). sglang-v0.5.18-rocm700-mi30x,
         2026-09-11, jobs 632594 (defect), 632837 (fix, PR #35 merged).
```

## See also

- [`aiter.md`](aiter.md): the fused MoE and skinny GEMM kernels these extensions implement, and AITER's own (unaffected) JIT cache
- [`weight-loading.md`](weight-loading.md): boot-time breakdown for the weight-load and graph-capture stages
- [`floor.md`](floor.md): the validated launch recipe and pitfalls index
