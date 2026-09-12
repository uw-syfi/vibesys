# Unified memory on MI300A

Scope: SKU `MI300A` (property: unified-memory), gfx942. Status: verified unless marked. Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-08-25 to 2026-09-10, Qwen3.5-397B-A17B-MXFP4, TP=4.

Purpose: how host/device memory unification on MI300A interacts with checkpoint loading, KV-pool sizing, and AITER's memory-fraction math, and the flags that make it reproducible.

## Mechanism

MI300A is an APU: page cache and HBM are one pool, host and device memory are physically the same DRAM. Bytes parked in page cache (mmap'd safetensors, prefetched checkpoint reads) compete with resident model weights and with the scheduler-init KV-cache and Mamba-cache allocation.

None of this applies to MI300X: it is discrete, so host page cache and device HBM are separate pools and this contention does not exist. Every finding in this file is scoped to MI300A (the unified-memory property), not to gfx942 as a whole.

Scope: SKU MI300A. Status: verified (public architecture fact, confirmed by the load-path behavior below). Stamp: public spec (APU unified memory) plus `sglang-v0.5.18-rocm700-mi30x` observation, 2026-08-25 to 2026-09-10.

## HF-checkpoint load recipe

Conditions: 239 GB MXFP4 checkpoint (amd/Qwen3.5-397B-A17B-MXFP4), TP=4, 4x128 GB MI300A node.

| `num_threads` | Drop cache after load | Outcome |
|:--|:--|:--|
| 2 | No | Loads in 218 s; ~185 GB free after load. Recommended. |
| 2 | Yes | Loads in 258 s. |
| 4 | No | Loads in 122 s, but OOM-killed on one of two boots (unstable). |
| 4 | Yes | Crashes at scheduler init: uneven per-rank free memory, Mamba cache sizing goes negative. |
| 8 | Yes | Same crash as 4 threads with drop-cache. |

Recommended flags: `--weight-loader-disable-mmap` and `--model-loader-extra-config '{"num_threads": 2}'`, no drop-cache flag.

Scope: MI300A, `sglang-v0.5.18-rocm700-mi30x`, TP=4, 239 GB MXFP4 checkpoint. Status: verified. Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-05, jobs 623402, 623405, 623406, 623408, 623409.

`--weight-loader-prefetch-checkpoints` hoards page cache and recreates the same contention; rejected on this platform. Scope: MI300A. Status: verified. Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-05.

## KV-pool profiling instability and the pin

SGLang sizes the KV pool from free memory measured after weight load (`kv_cache_configurator._profile_available_bytes`):

```
rest = free_after_load - pre_load_free * (1 - f) - mm_reservation
```

Page-cache residue at profile time differs by load path on a unified-memory device, so `free_after_load` varies across otherwise-identical boots: the HF-checkpoint path floated 780k to 900k tokens across boots; the `sharded_state` path reached 1.76M tokens (no page-cache residue at profile time), which lengthened CUDA-graph capture to 136 s (versus 34 to 50 s) and left 9 GB free.

Fix: pin `--max-total-tokens` to a validated baseline (787936 here) so serving is comparable across loaders. Fork commit 23d54bbba1 (uw-syfi/sglang) adds this pin.

Scope: MI300A, `sglang-v0.5.18-rocm700-mi30x` with the aiter attention backend. Status: verified. Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-09 to 2026-09-10, jobs 631xxx.

## AITER's mem-fraction multiplier and save-time memory math

SGLang multiplies `mem_fraction_static` by 0.85 when `attention_backend == "aiter"` and context length exceeds 8192 (`server_args.py`). An input of 0.72 becomes an effective 0.612.

This shapes the memory budget for saving a pre-sharded `sharded_state` artifact (`Engine.save_sharded_model`): the save needed `mem_fraction_static=0.85` (effective 0.7225 after the multiplier) to succeed. `0.35` and `0.72` both failed with:

```
Not enough GPU memory for hybrid (mamba/linear-attention) state cache
```

(`max_mamba_cache_size` computed negative), because the pool must cover resident weights plus the mandatory Mamba state cache. The save step also needs `max_running_requests=8` and `disable_cuda_graph=True`; non-weight files (config, tokenizer, etc.) are copied separately and are not part of the sharded artifact. See [`weight-loading.md`](weight-loading.md) for the artifact layout and the load-side fast path.

Scope: MI300A, aiter attention backend, Qwen3.5-397B-A17B (hybrid attention/Mamba model). Status: verified. Stamp: `sglang-v0.5.18-rocm700-mi30x`, job 631025, 2026-09-10.

## Pitfalls

### Scheduler-init crash from drop-cache at higher thread counts

```
Symptom: rank init crashes at scheduler start; per-rank free memory is
         uneven, and the computed Mamba cache size goes negative.
Cause:   dropping the page cache after load, combined with 4 or 8 loader
         threads, races the free-memory snapshot SGLang uses to size the
         Mamba state cache, on a unified-memory (page-cache == HBM)
         device.
Fix:     use --weight-loader-disable-mmap with
         --model-loader-extra-config '{"num_threads": 2}' and no
         drop-cache flag (218 s load, ~185 GB free after load).
Scope:   MI300A, sglang-v0.5.18-rocm700-mi30x, TP=4, 239 GB MXFP4
         checkpoint.
Status:  verified. sglang-v0.5.18-rocm700-mi30x, 2026-09-05, jobs
         623402, 623405, 623406, 623408, 623409.
```

### Sharded-artifact save needs mem-fraction 0.85

```
Symptom: Engine.save_sharded_model fails: "Not enough GPU memory for
         hybrid (mamba/linear-attention) state cache"; max_mamba_cache_
         size computed negative.
Cause:   AITER's 0.85x mem_fraction_static multiplier (context > 8192)
         shrinks the effective pool further; the pool must still cover
         resident weights plus the mandatory Mamba state cache.
Fix:     set mem_fraction_static=0.85 for the save step (effective
         0.7225) when the target itself loads from the raw HF
         checkpoint during the save; 0.35 and 0.72 both fail there with
         the GPU-side error above.
Scope:   MI300A, aiter attention backend, sglang-v0.5.18-rocm700-mi30x,
         Qwen3.5-397B-A17B (hybrid attention/Mamba); target loaded from
         the raw HF checkpoint during the save. A different failure
         applies when the target loads from an already-sharded artifact
         and only a draft model loads through the HF safetensors loader
         inside a direct Engine() save script: a rank scheduler is
         OOM-killed on the host side (exit code -9) during "Multi-thread
         loading shards" at both 0.85 and 0.72, while the serving launch
         path loads the same draft at 0.72 without incident. The
         mem-fraction value is not the lever there: the cause is
         `weight_loader_disable_mmap=True` left on in that save script,
         which makes every rank read and deserialize its own private
         copy of every shard instead of sharing mmap'd pages across
         ranks; see [`weight-loading.md`](weight-loading.md)'s matching
         pitfall for the mechanism and fix.
Status:  verified. sglang-v0.5.18-rocm700-mi30x, job 631025, 2026-09-10
         (0.85 required, target loads from the HF checkpoint); jobs
         633543 and 633650, 2026-09-12 (host OOM in the direct-Engine
         draft save, cause: `weight_loader_disable_mmap=True`; fixed and
         confirmed in job 633711).
```

### KV-pool size drifts across otherwise-identical boots

```
Symptom: the max total tokens the pool auto-sizes to varies by more than
         2x across boots and loader paths (780k-900k HF versus 1.76M
         sharded), and CUDA-graph capture time varies with it (34-50 s
         versus 136 s).
Cause:   kv_cache_configurator._profile_available_bytes sizes the pool
         from free memory measured after load; page-cache residue at
         profile time differs by loader path on a unified-memory device.
Fix:     pin --max-total-tokens to a validated baseline (787936 here) so
         serving is comparable across loaders.
Scope:   MI300A, sglang-v0.5.18-rocm700-mi30x.
Status:  verified. sglang-v0.5.18-rocm700-mi30x, 2026-09-09 to
         2026-09-10, jobs 631xxx.
```

### Prefetching checkpoints recreates page-cache contention

```
Symptom: no crash, but the load-path contention this file documents
         reappears (page-cache pressure against resident weights and
         KV/Mamba allocation).
Cause:   prefetching checkpoint bytes into page cache ahead of load
         hoards the same unified pool weights and KV/Mamba cache need.
Fix:     do not use --weight-loader-prefetch-checkpoints on MI300A;
         rejected on this platform.
Scope:   MI300A, sglang-v0.5.18-rocm700-mi30x.
Status:  verified. sglang-v0.5.18-rocm700-mi30x, 2026-09-05.
```

## See also

- [`floor.md`](floor.md): the validated launch recipe this file backs
- [`weight-loading.md`](weight-loading.md): checkpoint materialization and the sharded-artifact fast path
- [`aiter.md`](aiter.md): the mem_fraction_static x0.85 multiplier's source
- [`hardware.md`](hardware.md): MI300A versus MI300X memory model
