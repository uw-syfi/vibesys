# SGLang source-code lookup

Short reference into `repos/sglang/`. Depends on the `skills/serving-systems/repos/sglang` submodule being initialized.

## Setup

```bash
export SERVE_REPOS=<vibesys-root>/skills/serving-systems/repos
# or substitute $SERVE_REPOS inline below.
```

If `$SERVE_REPOS/sglang/` is missing (e.g. running inside a fresh agent sandbox where the submodule isn't mounted), fetch only the pinned commit this skill was authored against — the paths and line numbers in the tables below assume it:

```bash
mkdir -p "$SERVE_REPOS/sglang" && cd "$SERVE_REPOS/sglang"
git init -q
git remote add origin https://github.com/sgl-project/sglang.git
git fetch --depth 1 origin 04b1caf75b3c6f043a979ddce21d43ed07c217a6
git checkout -q FETCH_HEAD
```

(From the vibesys repo root the equivalent is `git submodule update --init --checkout resources/skills/serving-systems/repos/sglang`.)

## Directory map

```
sglang/
├── python/sglang/
│   ├── launch_server.py                          # top-level launcher
│   ├── srt/                                      # the serving runtime
│   │   ├── managers/
│   │   │   ├── scheduler.py                      # main scheduling loop
│   │   │   ├── tokenizer_manager.py
│   │   │   └── detokenizer_manager.py
│   │   ├── mem_cache/
│   │   │   ├── radix_cache.py
│   │   │   ├── hiradix_cache.py
│   │   │   └── hicache_storage.py
│   │   ├── layers/
│   │   │   ├── attention/
│   │   │   │   ├── base_attn_backend.py
│   │   │   │   ├── attention_registry.py
│   │   │   │   ├── flashinfer_backend.py
│   │   │   │   ├── flashinfer_mla_backend.py
│   │   │   │   ├── cutlass_mla_backend.py
│   │   │   │   ├── flashattention_backend.py
│   │   │   │   ├── flashmla_backend.py
│   │   │   │   ├── triton_backend.py
│   │   │   │   ├── nsa_backend.py
│   │   │   │   ├── tbo_backend.py
│   │   │   │   ├── wave_backend.py
│   │   │   │   └── aiter_backend.py
│   │   │   ├── moe/
│   │   │   │   ├── router.py
│   │   │   │   ├── token_dispatcher/
│   │   │   │   ├── moe_runner/
│   │   │   │   └── ep_moe/                       # expert parallel + EPLB
│   │   │   └── quantization/                     # base_scheme.py, configs/, compressed_tensors/
│   │   ├── models/                               # per-model files
│   │   │   ├── deepseek_v2.py
│   │   │   ├── deepseek_nextn.py
│   │   │   └── deepseek_common/                  # shared DeepSeek components
│   │   ├── speculative/                          # eagle_worker.py, base_spec_worker.py, eagle_utils.py
│   │   ├── disaggregation/                       # encode_server.py, decode.py, base/
│   │   ├── distributed/                          # parallel_state.py, communication_op.py, device_communicators/
│   │   ├── compilation/                          # compile.py, cuda_piecewise_backend.py, compiler_interface.py
│   │   ├── model_loader/                         # loader.py, weight_utils.py
│   │   ├── entrypoints/
│   │   │   ├── engine.py
│   │   │   └── openai/serving_chat.py
│   │   ├── lora/                                 # lora_manager.py, layers.py, backend/
│   │   └── hardware_backend/                     # cuda / rocm / mlx / musa / npu adapters
│   └── jit_kernel/                               # Python Triton + CuTeDSL kernels
└── sgl-kernel/
    ├── csrc/                                     # attention, moe, quantization, kvcacheio, mamba, gemm, ...
    ├── include/
    └── python/
```

## Where's X?

| Need | Path (under `$SERVE_REPOS/sglang/`) |
|:-----|:------------------------------------|
| Scheduler, TokenizerManager, DetokenizerManager | `python/sglang/srt/managers/{scheduler,tokenizer_manager,detokenizer_manager}.py` |
| Radix cache + HiCache | `python/sglang/srt/mem_cache/{radix_cache,hiradix_cache,hicache_storage}.py` |
| Attention backend base + registry | `python/sglang/srt/layers/attention/{base_attn_backend,attention_registry}.py` |
| Individual attention backends | `python/sglang/srt/layers/attention/*_backend.py` (see dir map) |
| MoE routing + dispatch | `python/sglang/srt/layers/moe/{router.py,token_dispatcher/,moe_runner/}` |
| EPLB (expert load balancing) | `python/sglang/srt/layers/moe/ep_moe/` |
| Quantization | `python/sglang/srt/layers/quantization/{base_scheme.py,configs/,compressed_tensors/}` |
| Model implementations | `python/sglang/srt/models/` |
| DeepSeek V2 / V3 | `python/sglang/srt/models/deepseek_v2.py`, `.../deepseek_common/` |
| Speculative decoding (EAGLE) | `python/sglang/srt/speculative/{eagle_worker,base_spec_worker,eagle_utils}.py` |
| Disaggregated serving | `python/sglang/srt/disaggregation/` |
| Distributed (TP/PP/EP) | `python/sglang/srt/distributed/{parallel_state,communication_op}.py` |
| CUDA graph + piecewise compile | `python/sglang/srt/compilation/{compile,cuda_piecewise_backend,compiler_interface}.py` |
| Model loader / weight mapping | `python/sglang/srt/model_loader/{loader,weight_utils}.py` |
| Engine + OpenAI server entrypoints | `python/sglang/srt/entrypoints/engine.py`, `.../openai/serving_chat.py` |
| Launcher | `python/sglang/launch_server.py` |
| LoRA | `python/sglang/srt/lora/{lora_manager.py,layers.py,backend/}` |
| Hardware backend adapters | `python/sglang/srt/hardware_backend/` |
| JIT Triton / CuTeDSL kernels | `python/sglang/jit_kernel/` |
| Custom CUDA kernels (sgl-kernel) | `sgl-kernel/csrc/` |
| ShardedStateLoader, `sharded_state` format | `python/sglang/srt/model_loader/loader.py` (`class ShardedStateLoader`) |
| KV pool sizing from free memory after load | `python/sglang/srt/mem_cache/kv_cache_configurator.py` (`_profile_available_bytes`) |
| aiter mem-fraction 0.85 multiplier | `python/sglang/srt/server_args.py` (applied when `attention_backend == "aiter"` and context length > 8192) |
| Quark MXFP4 MoE scheme | `python/sglang/srt/layers/quantization/quark/schemes/quark_w4a4_mxfp4_moe.py` |
| Health-check timeout env (`SGLANG_HEALTH_CHECK_TIMEOUT`) | `python/sglang/srt/entrypoints/http_server.py` (`HEALTH_CHECK_TIMEOUT`, default 20 s) |
| `Engine.save_sharded_model` | `python/sglang/srt/entrypoints/engine.py` |

Status: verified, paths confirmed against an sglang checkout past the pinned commit above (commit `ae6ef906d9`), 2026-09-10.

## Grep anchors

Attention backend base + registration:
```bash
rg "class AttentionBackend|register_attention_backend|ATTENTION_BACKENDS" \
   $SERVE_REPOS/sglang/python/sglang/srt/layers/attention
```

Scheduler batch selection:
```bash
rg "def get_next_batch_to_run|def _get_new_batch_prefill" \
   $SERVE_REPOS/sglang/python/sglang/srt/managers/scheduler.py
```

Radix cache:
```bash
rg "class RadixCache|match_prefix|insert" \
   $SERVE_REPOS/sglang/python/sglang/srt/mem_cache/radix_cache.py
```

DeepSeek MoE routing wiring:
```bash
rg "class MoEGate|class DeepseekV2MoE|def routed_experts" \
   $SERVE_REPOS/sglang/python/sglang/srt/models/deepseek_v2.py
```

Speculative decode verify / accept:
```bash
rg "def verify|class.*SpecWorker|acceptance" \
   $SERVE_REPOS/sglang/python/sglang/srt/speculative/eagle_worker.py
```

Engine / launcher entry:
```bash
rg "class Engine|def launch_engine|launch_server" \
   $SERVE_REPOS/sglang/python/sglang/srt/entrypoints/engine.py \
   $SERVE_REPOS/sglang/python/sglang/launch_server.py
```

MoE token dispatcher:
```bash
rg "class.*TokenDispatcher|def dispatch|def combine" \
   $SERVE_REPOS/sglang/python/sglang/srt/layers/moe/token_dispatcher/
```

Disaggregation encode / decode servers:
```bash
rg "class EncodeServer|class DecodeServer|transceiver|KVSender|KVReceiver" \
   $SERVE_REPOS/sglang/python/sglang/srt/disaggregation/
```

Quantization method dispatch:
```bash
rg "class.*QuantScheme|get_quant_method|apply_weights" \
   $SERVE_REPOS/sglang/python/sglang/srt/layers/quantization/
```

## Request admission timing

A per-request-joined decomposition of turn-2+ TTFT (client timestamp joined to the scheduler's own `ReqTimeStats`/`request.finished` markers by request id) isolates where time goes between a request reaching the tokenizer manager and it landing in the scheduler's `waiting_queue`, and separates that from actual admission delay:

- `wait_queue_entry_time` (set in `managers/scheduler.py` when a request is pulled off the inbound ZMQ queue and appended to `waiting_queue`) only advances when the scheduler's own inbound-socket poll runs, and that poll is gated behind whatever other per-iteration work the current iteration is doing (batch selection, forward, sampling, detokenize dispatch). Under load this receipt-to-queue-arrival term averaged about half an iteration and reached a whole iteration at p95 (55 ms p50 / 204 ms p95 at 48 uncapped sessions, 22 ms p50 / 126 ms p95 at a 16-session cap), while tokenize-plus-ZMQ-dispatch alone measured flat at 9-10 ms p50 both concurrencies: nearly all of this term's size and its concurrency-scaling is the poll cadence, not the client or the transport.
- `PrefillAdder`'s own admission-rejection counters (`NO_TOKEN` vs `OTHER`) distinguish an admission-budget rejection from any other reason a candidate prefill batch does not admit a request. Over 16830 iterations (964 PREFILL, 15866 DECODE) on a 48-session multi-turn workload, `NO_TOKEN` rejections were zero: `schedule_conservativeness` is not a lever on an admission-budget problem when there isn't one, and queue wait itself measured near zero (0.6 ms p50 / 1.1 ms p95) in the same run.

See [`../tooling/performance-modeling.md`](../tooling/performance-modeling.md) for the general bucket-decomposition method this uses, and [`../models/qwen3-5.md`](../models/qwen3-5.md) for the full bucket table.

Scope: sglang, any backend (scheduler behavior, not platform-specific). Status: verified (measured via a request-id join, residual near logging precision; mechanism read from the recv-loop/`PrefillAdder` gating in `managers/scheduler.py`). Stamp: sglang fork at `b6f3d5d6c8`, 2026-09-12, job-verified.

### Host-side cost model, concurrency-1 (no queueing)

A finer, exact-rid-joined decomposition at concurrency 1 (idle server, no admission queueing, so every millisecond measured is a genuine per-request cost rather than a queueing artifact) attributes the scheduler-to-detokenizer chain, plus the `TokenizerManager` stages upstream of it, stage by stage:

| Stage | Cost | Notes |
|:--|:--|:--|
| Scheduler pickup (`recv -> schedule_chosen`) | under 1 ms | `IdleSleeper`'s `zmq.Poller.poll(1000)` is genuinely event-driven: pickup cost stays under 1 ms whether or not the scheduler had been idle beforehand, whether idle 9 ms or several seconds |
| `ForwardBatch` build | 1-2 ms | padding and slot-index construction |
| Target forward | dominant term | see [`../platforms/`](../platforms/) for the GPU-side split |
| `ModelRunner.sample()` | under 0.2 ms | greedy argmax at bs=1 is near-instant; a much larger profiler-based estimate for this stage does not reproduce here |
| NEXTN draft-extend-for-prefill | 4-7 ms | the largest real host-side lever: a synchronous forward on the critical path before the first token can be sent |
| Result processing + send to detokenizer | 2.5-5 ms | D2H sync, `next_token_ids.tolist()`, finish-state checks, ZMQ send |
| Chat-template render (`TokenizerManager`) | about 0.4 ms, flat | independent of conversation length |
| Tokenize (`TokenizerManager`, `_tokenize_texts`) | about 0.003 ms per prompt token | re-tokenizes the entire rendered conversation from scratch every turn, including already-tokenized history, so this term grows with total conversation length, not with the new-token count alone |
| Object construction (`_create_tokenized_object`) | 0.04-0.05 ms | `SamplingParams` build/normalize/verify, `array("q", input_ids)`, `TokenizedGenerateReqInput` construct; measured directly on the real classes at real request sizes, near-zero and flat |
| Cyclic GC pause overlap | ~0% at concurrency 1, 0.09-0.10% of summed span under c48 load | measured with `gc.callbacks`-based attribution; not a lever at this workload's allocation rate |
| IPC (pickle + ZMQ send to scheduler) | 0.09-0.36 ms (concurrency 1), up to about 0.56 ms p95 at c48 | corrects an earlier 3-6 ms estimate for this stage (see below); a pure-CPU pickle microbenchmark of a same-sized message with no server measured about 0.01 ms |

Under load (c48), the same stages preserve their ranking and every stage's own p95 widens versus its median from GPU contention across concurrent sessions, but `recv -> schedule_chosen` stays flat and small regardless of the scheduler's own idle fraction, confirming the idle-poll finding holds under load too.

**The earlier 3-6 ms IPC estimate was a bucket mislabel, not a measured IPC cost.** `request_built` (post-tokenize) to `sent_to_scheduler` is one coarse span in the client-facing decomposition; splitting it into 13 real sub-steps found the true pickle-plus-ZMQ-send cost near zero (the IPC row above) while the span's actual 3.44-5.76 ms (256/512/1024 extend tokens) sits inside a second, unrelated tokenization pass. `serving_chat.py` gates on `model_config.is_multimodal`, a model-capability flag true for any vision-capable checkpoint (this one included) regardless of a given request's own content, so a plain-text turn is decoded back from the `prompt_ids` `_tokenize_one_request` already computed via `tokenizer.encode`, sent to `TokenizerManager` as text, and re-encoded from scratch by the `Tokenize` stage above -- a full second tokenization pass over the same prompt on every no-media request. Candidate fix, `SGLANG_TEXT_ONLY_SEND_IDS` (send `input_ids` directly when a request carries no image/audio/video content): 0 of 253 turns mismatched against the text path on this checkpoint's tokenizer, drops the `request_built`-to-`sent_to_scheduler` span 94-96 percent (under 0.5 ms at every size), and improved pooled c48 p95 TTFT turn2+ 7.9 percent in one paired boot; default off (byte-identical when off), a draft PR is open, paired acceptance not yet scheduled. See [`../tooling/serving-benchmark.md`](../tooling/serving-benchmark.md) for a related tokenizer-side candidate (suffix-only incremental tokenization) and a `--tokenizer-worker-num` pitfall found while diagnosing this span.

This refines the receipt-to-queue-arrival term above: at concurrency 1 (so isolated from admission-queueing entirely), that term's own order of magnitude is dominated by `TokenizerManager`-side double tokenization of the current turn's prompt, not by IPC or anything inside the scheduler loop itself. See [`../models/qwen3-5.md`](../models/qwen3-5.md) for the full stage table and per-size numbers, and [`../tooling/serving-benchmark.md`](../tooling/serving-benchmark.md) for the low-overhead host-stamp method this uses instead of a profiler.

Scope: sglang, any backend (`TokenizerManager` and scheduler behavior, not platform-specific); measured on this hybrid GDN+MoE model under NEXTN k=3 speculative decode with the breakable prefill CUDA graph. Status: verified (exact-rid join at concurrency 1, monotonic stamps confirmed, stage sum reconstructs the measured total to within 1-2 ms; the double-tokenization mechanism confirmed by code reading and a live per-request diagnostic, not inferred). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

## Per-phase prefill CUDA graph: breakable backend accepted

`--cuda-graph-config` selects a capture backend independently per phase
(`decode`, `target_verify`, `draft_decode`, `draft_extend`, `prefill`, ...).
For the prefill phase this fork exposes four backends: `full`, `breakable`,
`tc_piecewise`, and `disabled` (the default: prefill always runs eager).
Locking a phase backend explicitly on the launch command bypasses
`server_args.py`'s own auto-disable cascade for that phase (the non-CUDA-hardware
rule and, discovered by this campaign, a multimodal-architecture rule), which
otherwise silently forces prefill graph capture off.

**`tc_piecewise` is a silent no-op under speculative decoding on this fork.**
This model_runner's `cuda_graph_setup.py` routes the target prefill forward
of an EAGLE-family speculative-decoding target (NEXTN counts as EAGLE-family
in this fork's own `spec_algorithm` classification) to the eager runner
whenever the configured backend is not `breakable`, citing a named FP4/MoE
decode-replay corruption bug (upstream issues #28386, #28870). The
client-visible prefill path never runs under a captured graph for
`tc_piecewise`, `full`, or `disabled` once a spec-decode target is active: a
fixed-prompt output-token-id check confirms this indirectly (`tc_piecewise`
is bit-exact against base, 24/24 tokens, consistent with never taking the
graph path). Do not read a lack of TTFT change on `tc_piecewise` under
spec-decode as a tuning miss; it is this routing rule, confirmed in source.

**`breakable` captures the target prefill and is accepted end to end.** A
single-request TTFT sweep found a real win at the small end of a 13-bucket
ladder (256 to 1024 tokens, step 64): 256 tokens 181.6 to 119.4 ms (-34
percent), 512 tokens 174.4 to 139.3 ms (-20 percent), shrinking to noise by
768-1024 tokens as the captured segment's own GPU compute (MoE routed +
dense GEMM) grows with token count and the graph has proportionally less
CPU-dispatch overhead left to hide (see
[`../tooling/performance-modeling.md`](../tooling/performance-modeling.md)
for the general rule this is one case of). Paired end-to-end acceptance
against the real multiturn harness:

| Concurrency | pooled p95 TTFT turn2+ | pooled p50 TTFT | median TPOT |
|:--|:--|:--|:--|
| 48 sessions, uncapped | -11.7 percent (363.6 to 321.1 ms) | -23.3 percent (195.6 to 150.1 ms) | -12.4 percent (12.94 to 11.34 ms) |
| 16-session cap | -29.5 percent raw, -32.4 percent collapse-excluded (292.9 to 206.4 ms) | n/a | -9.1 percent raw, -7.3 percent collapse-excluded |

Gates 13/13 every rep both concurrencies; boot time within budget at 48
sessions, a large swing at 16 sessions attributed to this cluster's own
sequential-boot variance, not the change (per-rank capture-elapsed lines show
the graph capture step itself cost only 58-60 s).

**Exactness cannot be certified token-for-token on this stack.** A fixed-prompt
check first found `breakable` diverging from base at token index 10 of 24.
A follow-up ran 5 fixed prompts x 3 repeats greedy at concurrency 1: base
matched its own repeat only 3 of 15 times (20 percent), and `breakable`
matched base 2 of 15 times (13 percent), not distinguishably worse than
base's own repeat-to-repeat rate at this sample size. Both sides show top-1
logprob differences up to about 0.5 nats on matching prefixes, far above
bf16-rounding-level (order 1e-2 to 1e-3): this stack's greedy output is not
run-to-run reproducible at concurrency 1 regardless of the graph backend.
`breakable` was accepted on the accuracy evaluation of record instead
(GSM8K level 1 and level-2 agreement, both inside the accepted tolerance
band; see
[`../tooling/accuracy-checker.md`](../tooling/accuracy-checker.md)), not on
token-level exactness.

**Capture cost is flat with bucket count.** Comparing 13-, 21-, and 4-bucket
ladders on the same boot: capture elapsed 48.88 s / 46.13 s / 26.46 s and
capture memory 16.04 / 16.48 / 15.43 GiB. Extending the ladder to 1536/2048
tokens gains only 5-15 percent there (not the 20-34 percent seen at
256-512), consistent with the same GPU-compute-growth effect above, not with
capture cost scaling with bucket count.

Scope: this fork, gfx942 MI300A TP=4, this hybrid GDN+MoE MXFP4 model under
NEXTN k=3 speculative decode with `--disable-overlap-schedule`, the aiter
attention backend, the fused MXFP4 MoE HIP extension path
(`SGLANG_MXFP4_MOE_HIP=1`). Status: `breakable` accepted (TTFT/TPOT paired
acceptance at two concurrencies plus the accuracy gate of record, all
passed); `tc_piecewise` refuted for this deployment (confirmed mechanism,
not a tuning miss); `full` not tested. Stamp: sglang-v0.5.18-rocm700-mi30x,
2026-09-13, job-verified.

## Pitfalls

### `profile_by_stage` cannot isolate a stage rarer than the ones around it

```
Symptom: capturing a turn-2+ prefill (EXTEND) trace with
         profile_by_stage=true never exports; a later POST /stop_profile
         returns 500 "Profiling is not in progress".
Cause:   the DECODE stage (far more frequent in a decode-heavy multi-turn
         workload) finishes and clears the tokenizer-manager's profiling
         flag before the rarer EXTEND stage this run wanted to capture
         ever runs.
Fix:     use profile_by_stage=false and time the profiling window
         manually so it brackets a request known to trigger the target
         stage (e.g. a fresh turn's prefill), instead of relying on
         stage-keyed auto-stop.
Scope:   sglang v0.5.18 fork, before PR #9 / #11 (PR #9 fixed a separate
         export bug in the same feature; this stage-isolation defect is
         still open as of PR #11).
Status:  verified (mechanism read from the profiling-flag lifecycle in
         source). sglang-v0.5.18-rocm700-mi30x, 2026-09-11.
```

### `--enable-mixed-chunk` is silently forced off whenever a speculative algorithm is set

```
Symptom: launch argv carries both `--enable-mixed-chunk` and a
         speculative-decoding algorithm; every iteration runs prefill
         and decode as separate forwards, and the mixed-chunk TTFT
         benefit disappears with no warning naming the interaction in
         the log.
Cause:   `is_mixed_chunk` requires `get_schedule().enable_mixed_chunk`
         (`managers/scheduler.py`), but `server_args.py` asserts
         `not enable_mixed_chunk` whenever `speculative_algorithm` is
         set (`server_args.py:9195-9197`). NEXTN resolves to the EAGLE
         family (`speculative_hook.py:52`), and `_handle_eagle_family`
         unconditionally sets `enable_mixed_chunk = False`
         (`speculative_hook.py:572-576`, with a warning, but not one
         that names this interaction) before that assert runs.
         `get_next_batch_to_run` already prefers a prefill batch over
         decode on every iteration regardless
         (`scheduler.py:3125-3131`, "Run prefill first if possible"),
         so once mixed chunk is off, an iteration runs either a
         prefill/extend batch or a decode/verify batch, never both.
Fix:     treat mixed chunk and speculative decoding as mutually
         exclusive in this engine version. Decide which one the target
         metric needs, and re-measure TTFT after enabling speculative
         decoding instead of assuming the mixed-chunk result carries
         over; do not rely on the boot log to flag the conflict.
Scope:   sglang, any backend (engine behavior, not platform-specific).
         Confirmed for NEXTN; other speculative algorithms route
         through the same EAGLE-family hook.
Status:  verified (mechanism read in source; a paired boot confirmed
         prefill and decode running as separate batches). sglang fork
         at b6f3d5d6c8, 2026-09-12, job-verified.
```

### The overlap scheduler's one-iteration publish lag can cost more than it saves once steps are long and TTFT-bound

```
Symptom: `--disable-overlap-schedule`, measured against the same NEXTN
         k=3 speculative-decode configuration with the overlap scheduler
         on: pooled p95 TTFT turn-2+ down 24.1 percent at 48 sessions
         uncapped (734 to 558 ms) and down 28.4 percent at a 16-session
         cap (439 to 314 ms), at a small but real cost, median TPOT up
         3.3 percent (37.2 to 38.5 ms) and 6.8 percent (13.2 to 14.1 ms)
         respectively.
Cause:   the overlap scheduler publishes a batch's first token one
         iteration after the batch that produced it, trading a
         published-result delay for keeping the device fed across
         iterations without waiting on the previous batch's
         post-processing. That trade is a net win when steps are short
         relative to the publish lag and the workload is
         throughput-bound. On a long-step configuration (here,
         NEXTN k=3 speculative decoding, about 110 ms per step) serving
         a TTFT-bound multi-turn workload, every first token pays the
         one-iteration publish lag in full, and the throughput headroom
         the overlap buys goes underused; turning it off puts the CPU's
         own per-step scheduling work back on the device's critical
         path instead of hiding it underneath the GPU's step, which is
         the small TPOT regression.
Fix:     do not assume the overlap scheduler is free on every workload.
         Rule: TTFT-weighted multi-turn workloads with spec decode, turn
         the overlap scheduler off; throughput-weighted workloads, keep
         it on. accept_len is unchanged either way (2.85 vs 2.86 of 4),
         confirming the flag changes only publish timing, not the
         draft/verify path.
Scope:   sglang, any backend (scheduler behavior, not platform-
         specific). Measured on a NEXTN (k=3) speculative-decoding
         configuration at two concurrencies; the publish-lag mechanism
         itself is general to the overlap scheduler, not specific to
         speculative decoding.
Status:  accepted. sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job-verified
         at both the 48-sessions-uncapped run (5 reps per side) and the
         16-session-cap run (5 reps per side), gates 13/13 every rep.
```

### Where the per-round host time goes with the overlap scheduler off

Elaborates the "Cause" above: the host-side gap between GPU kernels in one decode round (the busiest stream's idle time) splits into two roughly equal phases, not one. Measured at 16 concurrent sessions under the same NEXTN k=3, overlap-off configuration, the total host gap is 2.56 ms/round (about 4.5 percent of a 56.5 ms round):

| Phase | ms/round | Share of round | What dominates it |
|:--|--:|--:|:--|
| verify's last kernel -> next draft forward's first kernel | 1.42 | 2.5% | the scheduler's own per-iteration Python control-loop functions (request receive, batch-result processing, next-batch selection; roughly 1 ms combined), plus a CPU-side control-plane broadcast and copying results back to host (roughly 0.5 ms) |
| draft-KV-extend step -> next draft-loop's CUDA-graph replay | 1.10 | 1.9% | host dispatch of the next draft forward's Python/graph-replay path issuing while the GPU is still finishing the previous segment (0.7 to 1.0 ms), plus the graph-launch call itself (about 0.2 ms) |

These sub-mechanism figures overlap partially (a wrapper CPU annotation can enclose a more specific one measured separately) and are not strictly additive to the phase totals; the phase totals themselves are exact (clipped to each idle interval, no double counting). Neither phase is GPU compute: the rejection-sampling kernels that do run are cheap and already counted in the engine's own sampling/verify kernel bucket. The largest single software term is the scheduler's Python control-loop itself, at roughly 1 ms/round (1.7 percent of the round). This term, not GPU-side sampling, is what turning the overlap scheduler off exposes directly on the critical path (see the pitfall above): with the overlap scheduler on, this same host work runs on the following iteration underneath the GPU's own step instead of gating it.

Freshness (2026-09-13): a MoE kernel change (the permute-based MXFP4 decode fix, see [platforms/](../platforms/)) shrank the same decode round from 56.5 ms to about 51 ms. The two host-gap phases above are CPU-side scheduler work independent of the MoE kernel, so their absolute per-round cost is unchanged (1.42 ms and 1.10 ms); this section's percentages of the round are stale and not re-derived here.

Scope: sglang, any backend (scheduler behavior, not platform-specific). Measured under NEXTN k=3 with the overlap scheduler off, one concurrency (16 sessions); the two-phase split is expected to generalize to any speculative-decoding configuration pairing a verify step with a following draft-extend step, but the exact millisecond figures are specific to this batch size and draft length. Status: verified (measured via per-phase clipped-overlap event attribution against traced rounds). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job-verified.

A standalone script invoked outside the normal serving launch path (a
direct `Engine()` save or probe script, run by absolute path with no
`PYTHONPATH` set) can silently resolve `import sglang` to a different
install than the one it means to exercise, such as a container image's
own baked-in copy shadowing a staged checkout. This is general to
`sys.path` resolution, not backend-specific; see [platforms/](../platforms/)
for a worked example and fix.

### Admission-queueing collapse recurs at uncapped 48-session concurrency

```
Symptom: at uncapped (unbounded) concurrency around 48 sessions, a
         minority of reps show turn-1 (and occasionally turn-2) TTFTs
         spiking into the 3-15 second range for a growing subset of
         sessions, while rep wall-clock duration and the accuracy gate
         are unaffected; one affected rep measured mean TPOT about
         43 ms and p95 TTFT about 5.6 s against about 19 ms and 0.4 s
         in a clean rep. At the mechanism level the signature is one
         ordinary prefill batch running at 20x to 300x lower input
         throughput than same-size batches elsewhere in the same run, a
         queue backlog building behind it, with tail episodes
         clustering inside about 30 second windows.
Cause:   not fully diagnosed. Six candidates checked, all six now
         refuted: KV or Mamba pool pressure (no memory, OOM, or
         page-reclaim signal in any log); TunableOp online tuning
         (tuning is hard-disabled at serving time, zero tuning output
         logged, the table is read-only and unchanged); plain prefill
         saturation (the scheduler's own instrumented throughput field
         reports 15x to 300x lower tok/s for the slow batch than
         same-size batches moments earlier in the identical run);
         allocator refill after a cache flush, by timing (slow batches
         start 14 to 183 s after the preceding flush, not on the first
         post-flush batch, and recur several times within one flush);
         Triton autotune (every autotune key reachable from this
         model's prefill path is pinned to a fixed value, and the
         on-disk autotune cache shows zero writes inside any blackout
         window); an aiter tuned-shape-table miss versus a hit (a
         first-occurrence shape is no more likely to be slow than a
         repeat shape: 5 to 12 percent of first-occurrence batches are
         slow versus 0 to 7 percent of no-miss batches, and 4 of 10
         actual slow batches carry no shape miss at all, which also
         refutes first-touch shape cost as the main cause). The sixth
         and last live candidate, a silent caching-allocator retry
         inside the untuned GEMM fallback path, is now refuted too:
         appending `torch.cuda.memory_stats()` counters (retries, ooms,
         device alloc/free, reserved/allocated bytes) to every
         scheduler batch and `flush_cache` log line and reading them
         across 3911 logged observations (10 reps, one boot) found
         `retries` and `ooms` at zero everywhere, without exception,
         including at every rep-boundary flush (the largest deliberate
         allocator-stress event available) and at every one of 184
         milder slow-batch instances the run's own throughput-band
         proxy signature flagged; the only counter that ever moved
         (`dev_alloc`, by 1 to 6 at a time) moved with the same
         distribution on slow and normal batches, so it does not
         discriminate one from the other. Caveat: this run reproduced
         only the milder proxy signature (batches 4x to 20x below band
         median); it did not reproduce a severe multi-second blackout
         (p95 TTFT turn2+ never exceeded 392 ms in 10 reps), so the
         refutation covers the proxy signature's mechanism, not a
         confirmed severe episode under the same counters. With all six
         checked candidates refuted, the remaining candidates sit
         outside this process: host-side memory reclaim under pressure
         from something other than this process's own allocator,
         another tenant sharing the node, or a driver-level effect.
Fix:     none yet. Exclude affected reps symmetrically (same rule both
         sides of a paired comparison) and report both the raw and the
         collapse-excluded numbers rather than averaging over it
         silently; do not rely on `wait_for_idle` to catch it, since it
         reports the server idle before every rep regardless. Harness
         note: the rep-boundary cache-flush call itself costs the first
         request afterward about 6x a no-flush repeat (about 2.3 s at
         1024 tokens); this is unexplained and specific to this
         harness's rep-boundary flush, separate from the collapse
         mechanism above.
Scope:   sglang, uncapped concurrency around 48 sessions; not observed
         under a 16-session admission cap. Engine behavior, not
         platform-specific; see [`platforms/`](../platforms/) for the
         platform-specific detail behind each refuted candidate.
Status:  candidate (recurs across multiple separate jobs at this
         concurrency; root-cause mechanism not yet identified, all six
         checked candidates now refuted, including the allocator-retry
         candidate). What would verify a fix: a reproduction of the
         severe multi-second blackout itself (not only the milder
         throughput-band proxy) under the same
         `torch.cuda.memory_stats()` counter logging, to check the
         host-level candidates above against a real severe episode.
         sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.
```

## See also

- `engines/vllm/`, `engines/trtllm/`
- `algorithms/async-scheduling/` — SGLang's overlap scheduler (`event_loop_overlap`, `FutureMap`, `forward_stream` / `schedule_stream`) is the canonical "zero-overhead" implementation; the skill walks through the code
- `algorithms/*` — concepts behind each source location
- **FlashInfer** — used by FlashInfer + FlashInfer-MLA backends here
