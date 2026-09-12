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

Scope: sglang, any backend (scheduler behavior, not platform-specific). Status: verified (measured via a request-id join, residual near logging precision; mechanism read from the recv-loop/`PrefillAdder` gating in `managers/scheduler.py`). Stamp: sglang fork at `b6f3d5d6c8`, 2026-09-12, job 633804.

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
         at b6f3d5d6c8, 2026-09-12, job 633542.
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
Status:  accepted. sglang-v0.5.18-rocm700-mi30x, 2026-09-12, jobs 633754
         (48 sessions uncapped, 5 reps per side) and 633755 (16-session
         cap, 5 reps per side), gates 13/13 every rep.
```

A standalone script invoked outside the normal serving launch path (a
direct `Engine()` save or probe script, run by absolute path with no
`PYTHONPATH` set) can silently resolve `import sglang` to a different
install than the one it means to exercise, such as a container image's
own baked-in copy shadowing a staged checkout. This is general to
`sys.path` resolution, not backend-specific; see [platforms/](../platforms/)
for a worked example and fix.

## See also

- `engines/vllm/`, `engines/trtllm/`
- `algorithms/async-scheduling/` — SGLang's overlap scheduler (`event_loop_overlap`, `FutureMap`, `forward_stream` / `schedule_stream`) is the canonical "zero-overhead" implementation; the skill walks through the code
- `algorithms/*` — concepts behind each source location
- **FlashInfer** — used by FlashInfer + FlashInfer-MLA backends here
