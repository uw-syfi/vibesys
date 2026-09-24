# AMD profiler worklog

Progress and findings for AMD (ROCm) GPU profiling support in VibeSys, built
on branch `vic/amd-profiler`. This is a working log, not a spec: see
[Extend profilers](extending-profilers.md) for the general profiler-plugin
contract and `resources/skills/serving-systems/references/platforms/rocm/`
for the ROCm knowledge base this work produced. Update this file (add a dated
entry to the [Log](#log)) as the work continues; keep the top sections
current rather than growing them unboundedly.

## Status

| Area | State |
| --- | --- |
| ROCm knowledge docs (measurement protocol, counter triage, roofline, AITER engagement proof, profiler tool map) | Merged |
| `rocprof` `ProfilerKind`: MCP server, `rocprof.j2` prompt, ROCm backend default | Merged |
| `capture.py` (shared capture-lifecycle-driven `profile_*` tools; curated MCP surface) | Merged, tested against fake `rocprofv3`/`rocprof-compute` executables (no GPU) |
| `analyze_rocprof.py` (rocprofv3 system trace) | Merged, validated on real MI210 + vLLM traces |
| `counters.py` (PMC, <=4 counters/pass) | Merged, validated on real MI210 data |
| `att.py` (thread trace) | Merged, validated on real MI210 data (ROCm >=7.1 only) |
| `compute.py` (rocprof-compute doctor/profile/analyze) | Merged, validated on real MI210 data |
| `kernel_bench.py` (paired A/B microbenchmarking) | Merged |
| `analyze_torch_profile.py` extensions (certify/gemm_shapes/roofline) | Merged, validated on real MI210 + vLLM traces |
| `capture_ops.py`/`inject/sitecustomize.py` (`profile_ops`, generic torch.profiler capture) | Merged, validated on real MI210 + vLLM (offline single-process and `vllm serve` topologies), driven through the real MCP server |
| Regression + property test hardening | Merged, tests fail on pre-fix code |
| Real vLLM-trace validation of the analyzers | Done for `analyze_torch_profile.py`/`profile_ops`; `analyze_rocprof.py`/`capture.py` (rocprof side) tracked separately |
| VibeSys remote (SkyPilot/Slurm) execution with `rocprof` | Not started: remote execution only supports `--profiler none` today |

## What was built

VibeSys profilers work at five altitudes, from whole-run system trace down to
paired A/B microbenchmarks; each altitude costs more capture overhead than
the last, so the agent is expected to descend only as far as the evidence
demands.

| Altitude | Question answered | Tool (CLI module) |
| --- | --- | --- |
| System timeline | Host/device overlap, idle gaps, launch cost, HIP graph coverage, kernel-library selection (AITER/CK/hipBLASLt/Triton/torch-native) | `analyze_rocprof.py` |
| Counters (PMC) | Which hardware ceiling one specific kernel is against | `counters.py` |
| Kernel-internal | Full Speed-of-Light + roofline for one targeted kernel | `compute.py` (rocprof-compute) |
| Instruction-level | Per-instruction stalls inside one kernel, one compute unit | `att.py` (Advanced Thread Trace) |
| Paired A/B | Did a change actually move the needle, same session, drift-controlled | `kernel_bench.py` |

These follow the existing profiler-plugin structure
(`resources/profilers/<kind>/` analyzer CLIs + a FastMCP server + a prompt
template, registered as a `ProfilerKind`): `rocprof.j2` teaches the profiler
agent which tool answers which question, and the ROCm backend's default
profiler kind was switched from `torch` to `rocprof`. The torch analyzer
(`resources/profilers/torch/analyze_torch_profile.py`) gained `certify`,
`gemm_shapes`, and `roofline` subcommands so it can independently attribute
vLLM traces on ROCm hosts as well as CUDA ones.

`resources/profilers/rocprof/capture.py` (new) builds one `profile_*` MCP
tool per altitude above (`profile_timeline`, `profile_counters`,
`profile_kernel_deep`, `profile_instructions`; a `profile_ops` tool delegates
to the torch plugin's own `capture_ops.py`, staged alongside rocprof) on top
of the shared `capture_runtime` lifecycle
(`resources/profilers/_common/capture_runtime.py`): start, optionally wait
for `ready_command` + run `load_command`, stop with `stop_signal`, escalate
if needed. Each capture writes a `manifest.json` recording its `kind`; a
dispatching `summary(capture)` and `compare(a, b)` read that field to pick
the right analyzer instead of the agent needing to know which one to call.
`profiling_capabilities()` folds what used to be several separate planning
tools (`counter_plan`, `att_plan`, `counter_sets`, `compute_doctor`) into one
host-capability report, naming which tool each capability line gates. The
MCP surface is now curated (capture tools + drill-downs + the torch
cross-check tools, all accepting either a capture id or an explicit path)
rather than exposing every CLI subcommand 1:1; the underlying CLIs are
unchanged and still directly runnable.

## Verified platform facts

Environment: an MI210 node (gfx90a, 104 CUs) on a Slurm cluster, with
`rocm/6.4.1` and `rocm/7.2.0` modules on the host, plus a vLLM ROCm container
with ROCm 7.2.3 / torch 2.12 for serving-workload captures. rocprof-compute
version 3.1.0.

**Capture recipes that work:**

- **PMC counters**: `rocprofv3 -i <pmc.txt> --output-format csv json -d <out> -- <cmd>`,
  one `pmc:` line with at most 4 counters per invocation. Output always lands
  under `<out>/pmc_1/<hostname>/<pid>_*`, not verbatim under `<out>`.
- **ATT (thread trace)**: needs `rocprofv3` >= 7.1 (ROCm 6.4.1 has no
  `--att` flag at all) plus the separately released
  `rocprof-trace-decoder` shared library (not shipped in any ROCm module),
  pointed at via `--att-library-path <dir>` (the directory, not the `.so`
  file). `--att-buffer-size` wants a plain integer byte count, not a
  unit-suffixed string.
- **rocprof-compute**: drop any `-k`/kernel-name filter that assumes a
  literal kernel name: torch GEMMs dispatch through hipBLASLt/Tensile
  kernels (e.g. `Cijk_Ailk_Bljk_...`), and a filter that matches nothing
  fails silently (an empty per-pass CSV, then a downstream `KeyError` far
  from the real cause) rather than erroring up front.
- **vLLM capture under rocprofv3**: wrapping `vllm serve` (the HTTP server)
  does not work: rocprofv3 only flushes trace buffers on a clean process
  exit, and V1's forked `EngineCore` subprocess does not exit cleanly when
  the parent is killed, so no trace files are ever written. The working
  recipe is an offline, single-process capture:
  `VLLM_ENABLE_V1_MULTIPROCESSING=0` plus a script that builds `vllm.LLM(...)`
  directly and calls `.generate(...)`, so the whole process exits on its own
  once generation finishes. `--attach` to an already-running process also
  did not work in this environment.
- **torch.profiler capture of vLLM**: this vLLM build has no
  `VLLM_TORCH_PROFILER_DIR` env var wired to `profiler_config`, so
  `POST /start_profile` on a running server 404s. `LLM(profiler_config=...)`
  plus `start_profile()`/`stop_profile()` on an offline `vllm.LLM` works.
  `stop_profile()` writes the trace file correctly but the process can then
  hang for minutes before exiting (reproduces on ROCm 7.2.3 / torch 2.12);
  wrap the capture in an outer timeout and treat the trace file on disk as
  the success signal, not the process's exit code.

**Output formats, in brief:** rocprofv3 CSV traces are long/tidy (one row per
event or per kernel-dispatch x counter pair), not wide; a serving workload's
`--hip-runtime-trace` output is the large one (2-4x the kernel trace), and
`--output-format json` duplicates the CSVs' content into a JSON blob that can
run >1000x larger for no extra information; drop `json` unless something
specifically needs it. ATT's decoded `code.json` row schema is
`[ISA, _, LineNumber, Source, Codeobj, Vaddr, Hit, Latency, Stall, Idle]`,
confirmed from the decoder's own `header` field (see the Appendix for the
earlier, wrong assumption this replaced).

## Bugs found by real data

Validating each toolkit against real MI210 output (not synthetic fixtures)
found bugs that synthetic data did not exercise:

| Component | Bug | How caught | Test |
| --- | --- | --- | --- |
| `counters.py` | `SQ_INSTS_MFMA` silently ignored in the counter catalogue | Real PMC capture had a nonzero column the report dropped | `1b5d2606` |
| `counters.py` | Counter catalogue referenced a nonexistent 32-byte write counter; read/write granularity was wrong | Cross-checked catalogue against real `rocprofv3 --list-avail` output | `1b5d2606` |
| `counters.py` | Kernel duration required a separate `--kernel-trace` capture, though PMC counter-collection rows already carry `Start_Timestamp`/`End_Timestamp` | Real counter-collection CSV had timestamps the report never used | `1b5d2606` |
| `counters.py` | `counter_plan` emitted the wrong CLI form and output directory layout | Real capture's actual output layout (`pmc_1/<hostname>/<pid>_*`) didn't match the planned one | `a464844b` |
| `att.py` | ATT capture end to end broken on ROCm 7.x without the separately released `rocprof-trace-decoder` library | First real ATT attempt failed with a missing-library error | `e1d8e7d9` |
| `att.py` | `code.json` column mapping assumed the wrong schema (a docs-derived guess, not the decoder's real header) | Real decoded output's own `header` field disagreed with the docstring | `e1d8e7d9` |
| `att.py` | Columns resolved by position instead of header name | Property test varying column order caught a mismatch position-based indexing would miss | rocprof property-test hardening |
| `compute.py` | `-p`/workload-path handling was wrong | Real `rocprof-compute profile` run against the wrong path | `2764888b` |
| `compute.py` | A kernel-name filter (`-k gemm`) that matches zero kernels succeeds silently, then crashes downstream with a `KeyError` far from the real cause | Real run: torch GEMMs are Tensile `Cijk_*` kernels, not literal `"gemm"` | `2764888b` |
| `compute.py` | `compute_doctor` checked for `rocprofv3`, but `rocprof-compute` shells out to the deprecated legacy `rocprof` internally | Real `rocprof-compute profile` run printed ROCm's own legacy-tool deprecation warning | `587a4f1d` |
| `compute.py` | Fallback CSV-summary output was oversized when the real `analyze` path failed | Real fallback output on a real workload | `dd25c876` |
| `compute.py` | `analyze` output had no banner and no row cap | Real `analyze --max-stat-num` output was 1700 lines | `dd25c876` |
| `analyze_rocprof.py` | `_hbm_bytes` could go negative on inconsistent counters | Hypothesis property test over counter combinations | `aa6b05e8` |
| `counters.py` | MFMA utilization was reported as a raw issue-rate number, not a fraction of the hardware's peak MFMA throughput | Follow-up from the counter-catalogue fix; a raw rate has no ceiling to compare against | `c8bd1df8`, `ca18e474` |
| `counters.py` | `--flops` (a per-dispatch FLOP count) was divided by `duration_ns` summed across every merged dispatch, understating achieved TFLOP/s by roughly the dispatch count | Real fixture's 4096^3 bf16 GEMM (2 merged dispatches) read ~33%/~60 TFLOP/s of spec peak; correctly scaled it reads ~65%/~118 TFLOP/s, inside the 115-150 TFLOP/s this shape measures on real MI210 serving load | `87a78ae1` |
| `analyze_rocprof.py` | `kernels`/`families` computed "%GPU" as a naive sum of per-kernel durations, double-counting when kernels on different HW queues of the same GPU genuinely overlap | Real graph-mode vLLM trace has overlapping Queue 1/2/4 windows; `idle_gaps`/`host_idle` already used a merged-interval union instead | `d7ee5abc` |

## Open items / next steps

- **Real-trace validation of the analyzers**: `analyze_rocprof.py` and the
  torch analyzer still need validation against the real vLLM traces captured
  during this work (100-230 MB rocprofv3 CSVs; gzipped Kineto traces),
  including performance on files that size and any capture-guidance updates
  that fall out of it. One finding from this pass: the real graph-mode
  trace's Composable Kernel `FmhaFwdKernel` (grouped/varlen-mode causal
  attention) averages ~338ms/call over 27 calls, a >1000x outlier against
  every neighboring kernel on the identical capture (rocprofv3's own
  `kernel_stats.csv` corroborates the raw per-dispatch rows -- not a VibeSys
  aggregation bug). `families` now flags this class of outlier
  (`_outlier_family_note`) instead of reporting it as a plain %GPU line; the
  underlying question -- badly undersized launch grid vs. a rocprofv3
  dispatch-timing quirk for this kernel's launch shape -- is still open and
  needs a targeted `compute.py profile`/`att.py` capture on that kernel.
- **VibeSys remote execution gap**: SkyPilot/Slurm remote execution only
  supports `--profiler none` today, so `rocprof` cannot yet be selected from
  a remote VibeSys run on the cluster, even though every tool in this work
  has been verified to run correctly when invoked directly on the node. No
  end-to-end VibeSys run with the `rocprof` profiler agent has happened yet.
- **gfx942/gfx950 numbers**: everything validated so far is MI210 (gfx90a).
  Numbers referenced for MI300-class (gfx942) or newer hardware are spec-only
  and unverified against real captures.
- **`--attach`**: rocprofv3's `--attach PID` path to an already-running
  process did not work against this environment's vLLM container (the
  process never spins up the helper thread `--attach` needs). Not required
  by the offline-capture recipe above, but worth another look if a live
  server capture is ever needed.

## Log

### 2026-09-24

Initial worklog. Summarized the branch's work to date: ROCm knowledge docs,
the `rocprof` `ProfilerKind` and its five-CLI toolkit, real-MI210 validation
that found the bugs tabulated above, and property-test hardening with shared
hypothesis strategies (`tests/vibesys/loops/rocprof_strategies.py`). Current
targeted test slice (`rocprof`/`torch_profile` keyword match) passes: 362
tests. Real-vLLM-trace analyzer validation and the remote-execution gap
remain open; see Open items above.

Final review pass. Resolved the two open suspicious-number items above:
`counters.py`'s `--flops` per-dispatch/aggregate-duration mix-up (real fix,
~2x understated TFLOP/s) and `analyze_rocprof.py`'s naive-sum %GPU
denominator (real fix, latent double-counting under cross-queue overlap, did
not materially move the specific number investigated). The real graph
trace's `FmhaFwdKernel` >1000x-outlier duration is not a VibeSys bug --
rocprofv3's own `kernel_stats.csv` and the raw per-dispatch rows agree, and
the tool now flags this class of outlier instead of reporting it silently.
Targeted test slice now passes 369 tests.

MCP validation of `profiling_capabilities`, `profile_counters`, and
`profile_instructions` against a real MI210 running an offline single-process
vLLM Qwen3.5-9B workload, driven through the real stdio MCP server (not a
direct Python import). `profiling_capabilities` correctly reported
rocprofv3/ROCm version, the detected GPU agent (gfx90a, 104 CUs), ATT
available/unavailable in both directions (decoder found vs. a clear
fix-pointing message when the library path env var is unset), and
rocprof-compute absent with a clear fix. `profile_counters` ran two counter
sets against the real offline workload with a kernel filter on the top
Tensile GEMM kernels and, separately, the CK FMHA kernel: verdicts and
utilization numbers were physically sane (achieved HBM bandwidth ~52-53% of
spec peak on bandwidth-bound GEMM shapes; the CK FMHA kernel's measured MFMA
busy fraction at 77% of peak). Found and fixed a real bug this exercise
surfaced: `counters.py`'s MFMA compute-bound classifier fell back to an
uninterpretable, mislabeled "no peak reference" verdict for a kernel whose
peak-normalized MFMA busy fraction had actually been measured and simply
landed under the compute-bound threshold -- the honest low-utilization
verdict was being hidden behind advice to recapture a counter that was
already captured. `profile_instructions` (ATT) captured a decoded dispatch
end to end and produced a sane stall breakdown (VMEM-wait dominant, real
`v_mfma_f32_32x32x8bf16_1k` instructions present); its per-instruction source
lines came back `<unknown>`, expected since the precompiled Tensile/CK
kernels this workload dispatches ship without debug info, not a profiler
defect.

Validated the generic `torch.profiler` capture tool (`profile_ops`,
`resources/profilers/torch/{capture_ops.py,inject/sitecustomize.py,
server.py}`) against a real MI210 running vLLM with Qwen/Qwen3.5-9B, driven
through the real MCP server (a stdio JSON-RPC client, not a direct Python
call). Four checks:

- **Offline single-process capture** (no engine cooperation, `delay_s=0`,
  no `duration_s`): produces a real Kineto trace with GPU kernels and Input
  Dims; `certify` PASS/WARN with no FAIL; `gemm_shapes` correctly separates
  decode-phase GEMMs (M = batch size, K = hidden size 4096) from
  prefill-phase GEMMs (M = total prompt tokens). The generic tool needs no
  engine cooperation to produce a usable trace.
- **`delay_s`/`duration_s` window bounding** is mechanically precise
  (confirmed against a predictable synthetic GEMM workload): SIGUSR1/SIGUSR2
  fire almost exactly `delay_s`/`delay_s + duration_s` after arming. But
  `torch.profiler.profile().start()` itself took ~1.9-2.5s to actually begin
  recording after the handler ran, every time, on this stack; `duration_s`
  is measured from signal-send, not from when recording starts, so a short
  requested window loses a large fraction of its nominal length to this
  fixed cost. Documented in `inject/sitecustomize.py`.
- **Signal delivery latency is not the bottleneck**: measured well under
  100ms from `os.kill(SIGUSR1)` to the handler running, including under
  sustained GPU-launch load on another thread. The real latency is
  torch.profiler's own ROCm backend init (above).
- **Cross-thread CPU-op capture**: a background thread that predates
  `prof.start()` recorded 0% of its ops (the `cpu_op`/`record_shapes`
  attribution `gemm_shapes`/`roofline` need), while its GPU kernels were
  captured in full; an identical workload run on the main thread (so it
  necessarily starts after the signal-triggered `prof.start()`) recorded
  ops for 100% of its calls. No public torch.profiler option was found to
  fix already-running threads; documented with the concrete mitigation
  (arm early, `delay_s=0`, before the target spawns its own worker
  threads).
- **`vllm serve`-style multi-process topology** (API server + a separate
  engine-core process) with `ready_command`/`load_command`/
  `stop_signal=SIGINT`: the target exits cleanly with no escalation needed,
  and the chained SIGINT handler does not interfere with the engine's own
  graceful shutdown. Found and fixed a real bug: the engine-core process
  (the one doing the actual GPU work) armed and started profiling
  correctly, but `profile_ops` picked the primary trace immediately after
  the directly-launched API-server process exited, before the worker's own
  independent stop/export had produced a file — silently falling back to
  the near-empty driver-process trace with no error. Fixed with a bounded
  post-stop wait (`capture_ops.wait_for_additional_traces`, capped at
  `grace_s`) before trace discovery; regression and hypothesis property
  tests added (`tests/vibesys/loops/test_torch_capture_ops.py`), verified
  failing on the pre-fix code.

GPU-free unit/property tests for the torch profiler plugin (capture_ops,
inject/sitecustomize, analyze_torch_profile, the MCP server) pass: 155
tests across the targeted slice.

## Appendix: detailed format notes and commands

### rocprofv3 PMC counter output

Per-pass files under `<out>/pmc_1/<hostname>/<pid>_*`:

- `*_counter_collection.csv`: one row per (kernel dispatch, counter) pair
  (long/tidy, not wide). Columns include `Correlation_Id, Dispatch_Id,
  Agent_Id, Kernel_Name, Counter_Name, Counter_Value, Start_Timestamp,
  End_Timestamp`.
- `*_agent_info.csv`: one row per HSA agent (CPU + GPU), including
  `Cu_Count`, `Gfx_Target_Version`, `Product_Name`.
- `*_results.json`: the same data nested under one `"rocprofiler-sdk-tool"`
  key; large relative to the CSVs for no extra information.

Example plan command (via `counters.py plan`, never hand-written):

```
rocprofv3 -i pmc1.txt --output-format csv json -d pmc/pass1 -- <python> workload.py
```

with `pmc1.txt` containing a single `pmc: <counter1> <counter2> ...` line,
at most 4 counters.

### ATT (Advanced Thread Trace)

```
module load rocm/7.2.0
rocprofv3 --att --att-target-cu 1 --att-buffer-size 67108864 \
  --att-library-path <dir containing librocprof-trace-decoder.so> \
  -d <out_dir> --output-format csv json \
  --kernel-include-regex '<targeted kernel regex>' \
  -- <python> workload.py
```

Output: one `ui_output_agent_<PID>_dispatch_<N>/code.json` directory per
matched *and successfully decoded* dispatch; matching a kernel and decoding
it are not the same thing; some small/degenerate kernels match the include
regex but produce no decoded output. `code.json`'s own `header` field is
`ISA, _, LineNumber, Source, Codeobj, Vaddr, Hit, Latency, Stall, Idle`; the
sibling `stats_ui_output_agent_<PID>_dispatch_<N>.csv` has the same fields
under human-readable names. An earlier, docs-derived guess had labeled the
last field `issue_cycles`; the decoder calls it `Idle`.

An unfiltered `--kernel-include-regex '.*'` traces every matched dispatch in
the run (including setup/RNG-fill kernels), which produced a >100 MB output
directory for a small microbenchmark; target the specific kernel with a real
regex instead of tracing everything and filtering after decode.

### rocprof-compute

```
rocprof-compute profile -n <name> -p <out_dir> -- <python> workload.py
rocprof-compute analyze -p <out_dir> --max-stat-num 5
```

`profile` runs one replay pass per counter group the target architecture's
default metric set needs (16 passes for the default gfx90a set against a
~30s workload took ~6.5 minutes wall time, the real per-workload cost of
"profile everything," not startup overhead). Output includes `pmc_perf.csv`
(the joined per-dispatch counter table), `sysinfo.csv`, `roofline.csv`,
`timestamps.csv`, plus per-block CSVs and a `perfmon/` directory with the
generated legacy-`rocprof` input files per pass. `analyze` output has 18
numbered sections: 0 Top Stats, 1 System Info, 2 System Speed-of-Light, 3
Memory Chart, 4 Roofline, 5 Command Processor, 6 Workgroup Manager, 7
Wavefront, 10-18 cache/compute-unit blocks. Sections 8-9 are skipped
internally.

### rocprofv3 system trace of a serving workload

Offline single-process capture avoids the clean-exit requirement:

```
VLLM_ENABLE_V1_MULTIPROCESSING=0 \
rocprofv3 --kernel-trace --hip-runtime-trace --memory-copy-trace --stats \
  --output-format csv \
  -d <out_dir> \
  -- python3 offline_generate.py --model <model> [--enforce-eager] \
     --num-prompts <n> --max-tokens <n> --warmup-prompts <n> --warmup-tokens <n>
```

Run an untraced warmup `generate()` first (HIP graph capture happens there
in graph mode) so the traced window only contains steady-state kernels. On a
32-prompt/128-token batch against a ~9B-parameter model, `kernel_trace.csv`
reached ~100 MB (~220k rows) and `hip_api_trace.csv` ~230-380 MB (2.5-4M
rows); `--hip-runtime-trace` volume, not `--kernel-trace` volume, dominates
size and post-exit flush time (observed 2-4 minutes of wall time the
untraced run would not pay).

### torch.profiler capture of a serving workload

```python
llm = LLM(
    model=..., profiler_config={
        "profiler": "torch",
        "torch_profiler_dir": "<out_dir>",
        "torch_profiler_record_shapes": True,
    },
)
llm.generate(warmup_prompts, warmup_sampling)  # untraced warmup
llm.start_profile()
outputs = llm.generate(prompts, sampling)
llm.stop_profile()
```

Writes a gzipped Kineto trace (`rank0.<id>.pt.trace.json.gz`); graph mode
produces far fewer CPU-side events than eager mode, since HIP-graph replay
collapses many per-kernel dispatch events into one graph-launch event. In
graph mode, vLLM's own profiler wrapper additionally writes a human-readable
`key_averages().table()` dump.
