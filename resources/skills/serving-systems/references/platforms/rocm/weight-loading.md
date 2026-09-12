# Weight loading on ROCm

Scope: backend `rocm`, gfx942 tested. MI300A-specific memory constraints are called out inline and link to [`unified-memory.md`](unified-memory.md); they do not apply on MI300X (discrete). Status: verified unless marked. Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-08-25 to 2026-09-10.

Purpose: why checkpoint materialization time was dominated by the loader, not by I/O or compute, and the fork's fixes for the stock per-tensor MoE path and for pre-sharded artifacts.

## Stock per-tensor MoE materialization is the pathology, not I/O

Stock SGLang v0.5.18 loads MoE weights via per-tensor materialization: about 45 MB/s, roughly 60 min cold load, with CPUs and disks idle throughout.

Storage floor measured independently at 10.75 GB/s aggregate for a 239 GB read (22 s), so the loader, not I/O, was the bound.

Fork fix (uw-syfi/sglang commits fcff7c9dd8, bde3bde6be, 68a57d72f9): O(1) expert-tensor mapping plus threaded per-expert `weight_loader` dispatch. Result: 253 to 263 s, reproduced 5/5 across 2 nodes.

Scope: rocm, gfx942, `sglang-v0.5.18-rocm700-mi30x`, Qwen3.5-397B-A17B-MXFP4 (512-expert MoE), TP=4. Status: verified. Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-08-25, uw-syfi/sglang commits fcff7c9dd8, bde3bde6be, 68a57d72f9.

### The MTP draft class loads all 512 experts serially; boot with the draft adds minutes with disk and CPU idle

```
Symptom: booting with the NEXTN/MTP draft enabled adds about 8 minutes
         to boot versus the non-speculative recipe, with disk and CPU
         idle throughout the extra time; server.log shows the draft's
         own "Load weight end." line landing about 455 to 492 s after
         it starts, while the target's own load-weight phase stays at
         its usual 85 to 90 s.
Cause:   `Qwen3_5ForCausalLMMTP.load_weights` (`qwen3_5_mtp.py`,
         `load_fused_expert_weights`) loops `for expert_id in
         range(num_experts)` serially, one Python `weight_loader()`
         call per expert per projection (512 experts x 3 tensors each),
         with no threading. Per-rank draft memory usage is about 19 to
         23 GB, loaded in about 455 to 492 s: roughly 200 MB/s
         aggregate, far below the 10.75 GB/s storage floor measured
         above, so the load is dispatch-bound, not I/O-bound. This is
         exactly the pathology already fixed for the target model class
         (see "Stock per-tensor MoE materialization" above,
         `ThreadPoolExecutor`, `SGLANG_MOE_EXPERT_LOADER_WORKERS`); the
         fix was applied to the target class only, and the MTP draft
         class was never updated.
Fix:     two options, neither validated end to end yet: (1) a
         draft-only `sharded_state` artifact. The engine's
         `save_sharded_model` only saves the target
         (`weight_updater.py`, `weight_exporter.py`), so a
         `draft_worker` branch mirroring `save_remote_model`'s existing
         pattern must be added first; a fix along these lines is in
         progress (PR pending), and validation of the resulting draft
         artifact against a live boot is not yet complete. (2) apply
         the same threaded per-expert loader used by the target class
         to the MTP draft class.
Scope:   rocm, sglang-v0.5.18-rocm700-mi30x, Qwen3.5-397B-A17B-MXFP4
         NEXTN/MTP draft head (512-expert MoE at `mtp.layers.0`); the
         unthreaded loop is engine source, not platform-specific, but
         is recorded here because it is this platform's draft-boot
         cost.
Status:  verified cause (log timing); fix pending validation.
         sglang-v0.5.18-rocm700-mi30x, 2026-09-12, jobs 633510
         (boot-phase timing), 633511 (spec_k3_c48 boot).
```

### A direct `Engine()` save script with `weight_loader_disable_mmap=True` OOM-kills a rank on the host side

```
Symptom: a rank's scheduler subprocess dies during initialization (exit
         code -9) partway through "Multi-thread loading shards" while
         saving a draft model's shard from a direct `Engine(...)`
         construction, even though the identical draft checkpoint loads
         without incident at the same `mem_fraction_static` under the
         normal serving launch path.
Cause:   the save script set `weight_loader_disable_mmap=True` in its
         `Engine(...)` call. With mmap disabled,
         `multi_thread_safetensors_weights_iterator` does
         `open(path, "rb").read()` (a full private copy of the shard
         bytes) then `safetensors.torch.load(bytes)` (a second,
         separately deserialized tensor copy), both resident at once,
         independently inside each of the 4 per-rank scheduler
         subprocesses, with no cross-rank sharing of the unsharded
         checkpoint. The serving launch path loads the same checkpoint
         with the default `weight_loader_disable_mmap=False`, which
         mmaps each shard read-only and lets the OS share backing pages
         across all 4 ranks' mappings and evict clean pages under
         pressure.
Fix:     leave `weight_loader_disable_mmap` at its default (False) for
         a save script, matching the serving launch's loader
         configuration. Do not carry the flag over from the network-
         filesystem mmap-hang pitfall below: that one is scoped to
         `ShardedStateLoader` reading a sharded artifact over a network
         filesystem, not to the HF safetensors loader used here, and
         recommends the opposite setting for a different symptom. Do
         not apply one pitfall's fix to the other's symptom.
Scope:   rocm, MI300A (unified memory: host-side OOM), a direct
         `Engine()` construction that loads a draft model from the raw
         HF checkpoint while the target loads from an already-sharded
         artifact.
Status:  verified. sglang-v0.5.18-rocm700-mi30x, 2026-09-12, jobs
         633543, 633650 (OOM at mem_fraction_static 0.85 and 0.72 with
         the flag on); fix confirmed in job 633711 (flag off, save
         completed).
```

### `save_sharded_model`'s RPC swallows a scheduler-side exception and reports success

```
Symptom: a `save_sharded_model` call returns success (`SAVE_EXIT=0`,
         the calling script's own log ends cleanly) but the output
         directory holds none of the expected
         `model-rank-*-part-*.safetensors` files for one branch of the
         save (here, the draft branch of a target+draft save).
Cause:   the scheduler-side RPC handler raised inside
         `save_sharded_model` (an `AttributeError` from assuming an
         attribute chain the actual worker class doesn't have), and
         `Scheduler.handle_rpc_request` caught the exception, logged
         it, and returned `RpcReqOutput(success=False, ...)`; that
         failure did not reliably propagate back through
         `Engine.save_sharded_model`'s own `assert recv_req.success` to
         the calling script, which observed a clean, successful return
         despite the handler's own failure.
Fix:     never trust a save call's own reported exit status alone.
         After any `save_sharded_model` call, check the output
         directory for the expected rank/part files and fail the job
         if they are absent; log tensor and byte counts from inside the
         RPC handler itself, before and after the write, so a future
         silent failure shows up in the handler's own log rather than
         only in a downstream postcondition check.
Scope:   sglang, any backend (engine-level RPC defect, not platform-
         specific); recorded here because the failure surfaced on this
         platform's draft-model save path.
Status:  verified. sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job 633711
         (draft branch silently wrote zero tensors); fix (postcondition
         check plus in-handler logging) validated in job 633733.
```

## Pre-sharded artifact: sharded_state save

1. Call `Engine.save_sharded_model` under TP=4, producing 56 parts totaling 212 GB (4 GiB parts).
2. Set `mem_fraction_static=0.85` for the save step. This value is required by AITER's mem-fraction multiplier and the mandatory Mamba state cache; see [`unified-memory.md`](unified-memory.md) for the failure mode below this value.
3. Set `max_running_requests=8` and `disable_cuda_graph=True` for the save step.
4. Copy non-weight files (config, tokenizer, generation config, etc.) separately; they are not part of the sharded artifact.

Fork commit 369c69502e (uw-syfi/sglang) makes the task harness boot from this artifact when it exists (load path, `--load-format sharded_state`); the save itself is a one-time operator step.

Scope: rocm, MI300A (the mem-fraction constraint is MI300A/unified-memory-specific; the part layout and save-step flags apply to the rocm/aiter combination generally). Status: verified. Stamp: `sglang-v0.5.18-rocm700-mi30x`, job 631025, 2026-09-10.

## ShardedStateLoader: avoid mmap over a network filesystem

Stock v0.5.18 `ShardedStateLoader` mmaps part files. Over a network filesystem this page-faults heavily and hung for more than 10 minutes.

Fork fix (uw-syfi/sglang commits 13891c001a, 24b5f21055): read without mmap (120 s), then overlap reads with device copies via a 2-deep prefetch window (65 to 75 s).

Instrumentation showed device copies at about 30 GB/s and reads at 0.4 to 0.56 GB/s per stream: the remaining time is the storage read cap, not the copy. Striping the artifact 8-way evened per-rank completion time but did not raise the aggregate cap.

Scope: rocm, any network filesystem (site-independent). Status: verified. Stamp: `sglang-v0.5.18-rocm700-mi30x`, jobs 631192, 631193.

## The TP-sharded loader has no layout check; EP boots from the unsharded checkpoint and OOMs

```
Symptom: booting `--ep-size 4 --moe-a2a-backend none` OOM-kills a rank's
         scheduler process during initialization (exit code -9),
         reproduced independently on two nodes across a job requeue.
Cause:   the TP-sharded fast-path artifact is TP-layout-specific: each
         rank's shard holds a column slice of every expert at
         `intermediate_size / moe_tp_size`, not whole experts, so it does
         not match what an EP-configured MoE layer expects
         (`num_local_experts` whole experts at the full intermediate
         width). The sharded loader has no check of checkpoint layout
         against `moe_ep_size`/`moe_tp_size`: fed the TP-sharded
         artifact, it would silently narrow-copy the wrong weights
         rather than error (a shape mismatch only logs a warning before
         an unconditional copy into the destination tensor). The only
         currently-safe path is therefore booting EP from the original,
         unsharded checkpoint, and that load path exhausts per-rank host
         memory under EP=4 before the server comes up.
Fix:     no fix yet; blocked, not refuted. An EP-aware sharded artifact
         (produced the same way as the TP=4 sharded artifact, but split
         by whole experts rather than by intermediate-dim column) would
         restore a fast, layout-correct load path and is the next step
         before this configuration can be measured end to end. Until
         then, do not launch EP from the unsharded checkpoint on a
         memory-constrained host loader.
Scope:   rocm, MI300A (unified memory, the same host-loader path this
         file's stock-materialization and boot-time sections above
         already document as fragile on this SKU). Analytical
         prediction, not yet measured: roughly 6 to 14 percent TPOT
         improvement at batch 16 versus the accepted TP=4 layout.
Status:  blocked (boot-time OOM, reproduced on 2 nodes; not refuted).
         sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job 633509.
```

## Boot-time breakdown

From the sharded artifact, single node, checkout staged in node-local tmpfs (see below):

| Stage | Time |
|:--|:--|
| Stage checkout + imports | 19 s |
| Process spawn | 48 s |
| Weight load | 67 s |
| Memory-pool allocation | 6 s |
| Graph capture | 34 s |
| Warmup | 21 s |
| **Total** | **~202 s** |

Compare: about 430 to 500 s from the HF-checkpoint path; about 9 min for the original (pre-fork) recipe. A boot overlapping another node's load on the same shared filesystem measured 324 s.

Scope: rocm, MI300A, `sglang-v0.5.18-rocm700-mi30x`, sharded_state artifact, TP=4. Status: verified. Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-10 (composes the loader-level measurements from jobs 631192/631193 and 631025).

## Stage the checkout in node-local tmpfs

Importing sglang from a checkout on a network filesystem costs about 85 to 107 s cold (client metadata-cache misses over roughly 5700 files) versus about 20 s from node-local tmpfs. File-by-file copy to tmpfs is slower still (177 s); a `git archive` tarball extracts in seconds.

1. Produce a tarball of the checkout with `git archive` (or equivalent) rather than copying files one by one.
2. Extract the tarball into node-local tmpfs before import.
3. Point the process's working directory and import path at the tmpfs copy. Cold import drops from about 85 to 107 s to about 20 s.

Scope: rocm, any network filesystem (site-independent). Status: verified. Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-10 (contributes the "stage checkout + imports" row above).

## Graph capture is not cacheable across boots

CUDA/HIP graph capture is process-local and cannot be serialized or reused across boots; every boot re-captures (34 to 50 s for 14 to 16 batch sizes here), and the cost is compute-bound.

Scope: rocm. Status: verified (reasoned from the API surface; no serialization mechanism exists). Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-10.

## See also

- [`floor.md`](floor.md): the validated launch recipe's loader flags
- [`unified-memory.md`](unified-memory.md): the mem-fraction math behind the sharded-artifact save, and the KV-pool pin needed to compare loaders
- [`aiter.md`](aiter.md): the attention/MoE kernel path, independent of which loader is used
