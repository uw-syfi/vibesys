# ROCm (AMD Instinct) optimization floor

Scope: backend `rocm`. Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-08-25 to 2026-09-11.

CDNA shares the accelerator model with CUDA (dynamic shapes, per-kernel launch cost, and on discrete parts a separate device memory), so the *shape* of the floor matches NVIDIA's even though the libraries differ. MI300A is the exception on memory: host and device share one pool, see [`unified-memory.md`](unified-memory.md). Where a technique is identical apart from the library name, this file says so rather than restating it.

**Verified on:** MI300A, `sglang-v0.5.18-rocm700-mi30x` image, 2026-08-25 to 2026-09-11, Qwen3.5-397B-A17B-MXFP4 at TP=4. The launch recipe and pitfalls index below are scoped to gfx942 (MI300A); confirm against your ROCm and library versions before extending to gfx950.

## 1. Continuous batching

Identical to CUDA in both design and rationale: dynamic shapes are cheap, so eliminate padding via variable-length packing or paged KV.

- Contract: [`algorithms/continuous-batching.md`](../../algorithms/continuous-batching.md)
- The CUDA implementation transfers directly; substitute the attention kernel below.

## 2. Fused attention kernel

Never run naive `softmax(QKᵀ)V`. On CDNA the options are:

| Option | When |
|:--|:--|
| **AITER** | AMD's fused-op library; the closest analog to FlashInfer. First choice where it covers the variant. |
| **Composable Kernel (CK)** | One of the layers AITER is built from (alongside Triton, HIP, and ASM); use directly for variants AITER doesn't cover. |
| **Triton** | Portable fallback; runs on CDNA and is how vLLM/SGLang cover ROCm gaps. See [`frameworks/triton.md`](../../frameworks/triton.md). |
| **PyTorch SDPA** | Floor. Fused, available, slower than the above. |

- [`aiter.md`](aiter.md)

## 3. HIP graphs

The launch-overhead problem and its remedy are the same as CUDA's; the API is `hipGraph`. `torch.compile(mode="reduce-overhead")` drives it through the same PyTorch path used on NVIDIA.

Capture decode, keep prefill eager or bucketed. The shape-stability and address-stability requirements carry over unchanged.

## Then

1. **Paged KV**: [`algorithms/paged-attention.md`](../../algorithms/paged-attention.md); the design applies unmodified.
2. **Prefix caching**: [`algorithms/radix-prefix-caching.md`](../../algorithms/radix-prefix-caching.md).
3. **Chunked prefill**: [`algorithms/chunked-prefill.md`](../../algorithms/chunked-prefill.md).
4. **Quantization**: FP8 is native from MI300 (CDNA3); FP4 from CDNA4. See [`algorithms/quantization-schemes.md`](../../algorithms/quantization-schemes.md) and the HW floor in [`hardware.md`](hardware.md).

## Validated launch recipe (SGLang, MI300A, MXFP4 MoE)

A working configuration for Qwen3.5-397B-A17B-MXFP4 on 4x MI300A, TP=4. Verified as a working set; not individually ablated unless noted. Rationale for each line lives in the file it links to, not here.

Environment:

- `SGLANG_USE_AITER=1`: enable AITER attention/MoE dispatch. See [`aiter.md`](aiter.md).
- `SGLANG_USE_AITER_UNIFIED_ATTN=1`: unified attention path (works on gfx942). See [`aiter.md`](aiter.md).
- `AITER_FLYDSL_FORCE=1`: see [`aiter.md`](aiter.md); FlyDSL a4w4 fails to compile on gfx942, so this flag has no effect here and the Triton MXFP4 MoE fallback is what actually runs.
- `SGLANG_MAMBA_SSM_DTYPE=bfloat16`: Mamba state cache dtype; see the mem-fraction and Mamba-cache sizing interaction in [`unified-memory.md`](unified-memory.md).
- `ROCM_QUICK_REDUCE_QUANTIZATION=INT8`: **candidate**, not ablated against alternatives or against being unset.
- `AITER_JIT_DIR=<persistent warm dir>`: must survive across launches. See [`aiter.md`](aiter.md) (JIT cache).
- `SGLANG_HEALTH_CHECK_TIMEOUT=1800`: see [`aiter.md`](aiter.md) (pitfalls: lazy variant build).
- `SGLANG_MXFP4_MOE_HIP=1`: on by default in the fork's platform config, replaces the Triton MXFP4 MoE fallback with a from-scratch fused HIP kernel. See [`aiter.md`](aiter.md).
- `SGLANG_SKINNY_GEMM=1`: on by default in the fork's platform config, custom skinny bf16 GEMM for the dense projections at decode M; stacks with the line above. See [`aiter.md`](aiter.md).

Together with mixed chunked prefill, these two flags cut median TPOT about 80 percent and pooled p95 TTFT turn-2+ about 44 percent versus all three off, under the same admission-aware open-loop schedule (benchmark_version 3, job 632958). See [`../../models/qwen3-5.md`](../../models/qwen3-5.md) for the four-side matrix.

argv:

- `--attention-backend aiter`: see [`aiter.md`](aiter.md).
- `--page-size 16`
- `--mem-fraction-static 0.72`: see [`unified-memory.md`](unified-memory.md) for the effective value after AITER's multiplier.
- `--tp 4`
- `--max-total-tokens 787936`: the KV-pool pin; see [`unified-memory.md`](unified-memory.md).
- loader flags per load path: see [`weight-loading.md`](weight-loading.md).

Flag set sourced from SGLang's `amd_gpu.mdx` docs and the Qwen3.5 deployment snippet's MI355X+MXFP4 branch. That source recipe's `--disable-radix-cache` is deliberately **not** applied here: it exists for FP4 kernels on MI355X (gfx950), and this workload is multi-turn and depends on prefix reuse, which radix caching provides.

### Optional: NEXTN speculative decoding

Accepted on top of the recipe above, for a per-token-latency win at the cost of a slower boot. See [`speculative-decoding.md`](speculative-decoding.md) for the full argv, the k=3 choice, and the results.

- `--speculative-algorithm NEXTN --speculative-eagle-topk 1 --speculative-num-steps 3 --speculative-num-draft-tokens 4`: NEXTN using the checkpoint's own MTP head, k=3.
- `--enable-linear-replayssm-spec`: fast per-slot mamba-state replay for the hybrid Gated-DeltaNet layers, valid because NEXTN is a linear (topk=1) draft chain.
- `--speculative-draft-model-path <original checkpoint> --speculative-draft-load-format auto`: mandatory, not optional, on this checkpoint's load path; see [`speculative-decoding.md`](speculative-decoding.md) pitfalls.

Without a draft-only sharded artifact, adds 2.6 to 2.8x to boot time (about 800 to 900 s versus about 300 s): the draft head loads from the unsharded checkpoint and boot captures extra decode graphs for the draft path. With the draft-only sharded artifact (see [`weight-loading.md`](weight-loading.md)), spec-decode boot is about 365 s (about 6 min) instead of about 866 s (about 14.5 min). A deployment-time cost only either way; use the sharded artifact when one has been produced for the checkpoint in use.

### Optional: PyTorch TunableOp tuned dense GEMM

Accepted on top of NEXTN k=3 with the overlap scheduler off (above): median TPOT 38.05 to 22.86 ms at 48 uncapped sessions (-39.9 percent) and 14.31 to 12.44 ms at a 16-session cap (-13.1 percent), pooled p95 TTFT turn2+ flat at both, exact numerics, accept_len unchanged. See [`aiter.md`](aiter.md#pytorch-tunableop-accepted-for-the-dense-projections-and-lm-head) and [`aiter-tunableop.md`](aiter-tunableop.md) for the tuning recipe, the per-device filename pitfall, and the mechanism.

Environment (serving):

- `PYTORCH_TUNABLEOP_ENABLED=1`
- `PYTORCH_TUNABLEOP_TUNING=0`: replay only; tune nothing new at serving time.
- `PYTORCH_TUNABLEOP_FILENAME=<path with %d>`: one read-only tuned table per device ordinal, built ahead of time. See [`aiter-tunableop.md`](aiter-tunableop.md) for the per-device substitution rule and why a single shared path voided the first acceptance attempt.

Tuned-table step (one-time, before deployment): tune in a pure-torch process with `PYTORCH_TUNABLEOP_TUNING=1` over every M value the CUDA-graph capture set will dispatch, then copy the resulting per-rank CSVs into the serving image read-only. The table's validator header pins the exact PyTorch/ROCm/hipBLASLt/GPU stack and is silently ignored on a mismatch; regenerate on any change to that stack.

## Known pitfalls

One line per known pitfall; detail lives at the link.

- Server exits shortly after the first request; log shows a JIT build of a kernel variant. [`aiter.md#lazy-kernel-variant-build-kills-health-check-on-first-request`](aiter.md#lazy-kernel-variant-build-kills-health-check-on-first-request)
- Launch hangs forever on "waiting for baton release". [`aiter.md#stale-jit-lock-after-a-killed-launch`](aiter.md#stale-jit-lock-after-a-killed-launch)
- Scheduler-init crash with uneven per-rank free memory after dropping page cache post-load. [`unified-memory.md#scheduler-init-crash-from-drop-cache-at-higher-thread-counts`](unified-memory.md#scheduler-init-crash-from-drop-cache-at-higher-thread-counts)
- Sharded-model save fails: "Not enough GPU memory for hybrid (mamba/linear-attention) state cache". [`unified-memory.md#sharded-artifact-save-needs-mem-fraction-085`](unified-memory.md#sharded-artifact-save-needs-mem-fraction-085)
- KV pool auto-sizes to a different token count across otherwise-identical boots. [`unified-memory.md#kv-pool-size-drifts-across-boots`](unified-memory.md#kv-pool-size-drifts-across-boots)
- MoE weight loading crawls at tens of MB/s with CPU and disk idle. [`weight-loading.md#stock-per-tensor-moe-materialization-is-the-pathology-not-io`](weight-loading.md#stock-per-tensor-moe-materialization-is-the-pathology-not-io)
- Sharded checkpoint load hangs for minutes with no progress on a network filesystem. [`weight-loading.md#shardedstateloader-avoid-mmap-over-a-network-filesystem`](weight-loading.md#shardedstateloader-avoid-mmap-over-a-network-filesystem)
- A Triton dequant/GEMM kernel profiles at 4-5 percent of HBM bandwidth and retuning its config doesn't help. [`aiter.md#dequant-triton-kernels-measure-at-4-5-percent-of-hbm-bandwidth-not-bandwidth-bound`](aiter.md#dequant-triton-kernels-measure-at-4-5-percent-of-hbm-bandwidth-not-bandwidth-bound)
- A weight-streaming kernel sits far below HBM bandwidth but MemUnitStalled is near zero and VALU busy is under 50 percent. [`aiter.md#fused-mxfp4-moe-kernel-sits-well-below-hbm-bandwidth-but-is-not-memory-bound`](aiter.md#fused-mxfp4-moe-kernel-sits-well-below-hbm-bandwidth-but-is-not-memory-bound)
- The fused MXFP4 MoE kernel is still 56 to 96x its weight-bandwidth floor at prefill M (337-919 tokens), same issue-latency mechanism as at decode M; no alternative kernel beats it there either. [`aiter.md#prefill-m-microbenchmark-floor-gap-confirmed-at-higher-m-dispatch-threshold-refined`](aiter.md#prefill-m-microbenchmark-floor-gap-confirmed-at-higher-m-dispatch-threshold-refined)
- Log fills with "not found tuned config ... will use default config" for dense GEMMs. [`aiter.md#aiters-tuned-gemm-table-misses-every-dense-projection-on-mi300a`](aiter.md#aiters-tuned-gemm-table-misses-every-dense-projection-on-mi300a)
- A TunableOp-tuned dense-GEMM table only speeds up one TP rank; the others read nothing and the step waits for the slowest rank. [`aiter-tunableop.md#per-device-filename-substitution-voided-the-first-acceptance-attempt`](aiter-tunableop.md#per-device-filename-substitution-voided-the-first-acceptance-attempt)
- A burst of large, never-repeated prefill shapes looks like the cause of multi-second server stalls but isn't (about two orders of magnitude too small). [`aiter.md#cold-dense-gemm-shape-resolution-is-milliseconds-not-the-cause-of-multi-second-stalls`](aiter.md#cold-dense-gemm-shape-resolution-is-milliseconds-not-the-cause-of-multi-second-stalls)
- Custom HIP extensions rebuild from source on every fresh boot despite a persistent build-cache directory. [`boot-costs.md#the-hip-extension-loader-keys-staleness-on-path-and-mtime-not-content`](boot-costs.md#the-hip-extension-loader-keys-staleness-on-path-and-mtime-not-content)
- `rocprof-compute` exits during its own startup dependency check. [`profiler.md#rocprof-compute-fails-its-own-dependency-check-on-this-image-rocprofv3-works`](profiler.md#rocprof-compute-fails-its-own-dependency-check-on-this-image-rocprofv3-works)
- Server crashes at boot during decode graph capture with "operation not permitted when stream is capturing". [`aiter.md#a-host-sync-in-a-custom-kernels-dispatch-crashes-decode-graph-capture-at-boot`](aiter.md#a-host-sync-in-a-custom-kernels-dispatch-crashes-decode-graph-capture-at-boot)
- Speculative decoding's draft model doesn't find MTP weights unless pointed explicitly at the original checkpoint, with an explicit draft load format too. [`speculative-decoding.md#the-draft-head-has-no-sharded-fast-path-artifact-point-the-draft-at-the-original-checkpoint`](speculative-decoding.md#the-draft-head-has-no-sharded-fast-path-artifact-point-the-draft-at-the-original-checkpoint)
- Booting expert parallelism from the unsharded checkpoint OOM-kills a rank's scheduler during initialization. [`weight-loading.md#the-tp-sharded-loader-has-no-layout-check-ep-boots-from-the-unsharded-checkpoint-and-ooms`](weight-loading.md#the-tp-sharded-loader-has-no-layout-check-ep-boots-from-the-unsharded-checkpoint-and-ooms)
- Booting with the NEXTN/MTP draft adds about 8 minutes with disk and CPU idle: the draft class loads all 512 experts serially with no threading; fixed by a draft-only sharded artifact. [`weight-loading.md#the-mtp-draft-class-loads-all-512-experts-serially-boot-with-the-draft-adds-minutes-with-disk-and-cpu-idle`](weight-loading.md#the-mtp-draft-class-loads-all-512-experts-serially-boot-with-the-draft-adds-minutes-with-disk-and-cpu-idle)
- A direct `Engine()` save script OOM-kills a rank on the host side during "Multi-thread loading shards" even though the same checkpoint serves fine. [`weight-loading.md#a-direct-engine-save-script-with-weight_loader_disable_mmaptrue-oom-kills-a-rank-on-the-host-side`](weight-loading.md#a-direct-engine-save-script-with-weight_loader_disable_mmaptrue-oom-kills-a-rank-on-the-host-side)
- A standalone save or probe script exits 0 with nothing new to show, or raises for an argument the staged code accepts. [`weight-loading.md#standalone-engine-script-silently-imports-the-containers-stock-engine-instead-of-the-staged-checkout`](weight-loading.md#standalone-engine-script-silently-imports-the-containers-stock-engine-instead-of-the-staged-checkout)

## Where ROCm differs from CUDA

| Concern | Difference |
|:--|:--|
| Interconnect | Infinity Fabric, not NVLink. Lower per-pair bandwidth; no NVL72-equivalent domain. Affects TP sizing, see [`algorithms/parallelism.md`](../../algorithms/parallelism.md). |
| Memory capacity | MI300X ships 192 GB and MI325X 256 GB, larger than contemporary NVIDIA parts. Capacity-bound designs that need offload on NVIDIA may fit resident here. |
| Kernel coverage | Narrower than NVIDIA. Verify the specific attention variant and quantization scheme are implemented before designing around them. |
| Engine support | vLLM and SGLang support ROCm; TensorRT-LLM does not. |

## See also

- [`hardware.md`](hardware.md): MI300A / MI300X / MI350X specs, GFX IDs, precision support
- [`aiter.md`](aiter.md): AITER / Composable Kernel, capability by gfx target, JIT cache
- [`unified-memory.md`](unified-memory.md): MI300A load-path recipe, KV-pool pin, mem-fraction math
- [`weight-loading.md`](weight-loading.md): MoE weight materialization, sharded-artifact fast path
- [`boot-costs.md`](boot-costs.md): one-time boot/warmup costs and the HIP extension cache staleness pitfall
- [`profiler.md`](profiler.md): rocprofv3 / rocprof-compute
- [`speculative-decoding.md`](speculative-decoding.md): NEXTN/MTP recipe, k-choice, and load-path pitfalls
