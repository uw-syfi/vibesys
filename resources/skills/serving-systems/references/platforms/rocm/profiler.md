# Profiling on ROCm

Which `rocprof`-family tool answers which question, at which altitude, and
how to capture one cleanly against a running vLLM or SGLang server on CDNA.
The discipline is the portable one in [`tooling/profiler.md`](../../tooling/profiler.md)
(classify with a system timeline before descending); this file is the tool
substitution table plus the ROCm-specific mechanics.

> **Status.** The `rocprof` profiler kind is wired up: system trace, PMC
> counters, thread trace, kernel-internal, and paired A/B timing all have a
> workspace tool (`rocprof_profiler/`, below). PMC counter capture, tool
> inventory, **ATT thread-trace capture + decode**, **`rocprof-compute`
> kernel-internal `profile`/`analyze`**, and **end-to-end `rocprofv3` system
> trace of a real vLLM serving workload** are all verified on this repo's
> MI210 test cluster (ROCm 6.2.1 through 7.2.0 modules available; host venv
> runs ROCm 6.4.1 to match `torch 2.9.1+rocm6.4`; `rocprof-compute` version
> confirmed `3.1.0`; the vLLM capture used a ROCm 7.2.3 container). The
> `torch.profiler` pitfall in this file's Pitfalls section **is** measured on
> this repo's MI210 bundle. The capture recipe below reflects what actually
> worked, not just what the flags document — see the clean-exit requirement
> in step 2, which was the root cause behind an earlier "zero trace files"
> failure mode.

## Altitudes and tools

| Altitude | Answers | Tool | Where |
|:--|:--|:--|:--|
| System timeline | host/device overlap, idle gaps, launch cost, HIP graph coverage | `rocprofv3` system trace | `rocprof_profiler/analyze_rocprof.py` |
| Framework / op | which torch op dominates; which library a dispatch actually hit | `torch.profiler` (works unmodified on ROCm) | `resources/profilers/torch/analyze_torch_profile.py` — `certify`, `gemm_shapes`, `roofline` |
| Counters (PMC) | which hardware ceiling one kernel is against | `rocprofv3` hardware counters | `rocprof_profiler/counters.py` — `plan`, `report`, `triage` |
| Kernel-internal | full Speed-of-Light + memory chart + empirical roofline for one kernel | `rocprof-compute` (named `omniperf` before ROCm 6.3) | `rocprof_profiler/compute.py` — `doctor`, `profile`, `analyze` |
| Instruction-level | per-instruction stalls inside one kernel, on one CU | `rocprofv3` thread trace (ATT) | `rocprof_profiler/att.py` — `plan`, `hotspots`. **Version-gated: see Pitfalls.** |
| Paired A/B | did a change actually move the needle, same session | HIP event timing around an isolated replay | `rocprof_profiler/kernel_bench.py` |

Exact argument syntax for each tool lives in its own prompt/help text, not
here — this table is for picking the right one.

## Which one to reach for

```
Is the problem already known to be inside one kernel?
  ├─ YES → compute.py for that kernel's Speed-of-Light + roofline;
  │        att.py only if you then need per-instruction stall evidence
  └─ NO ─► Is it a Python / op-dispatch / library-selection question
           (which kernel library actually ran)?
             ├─ YES → torch profiler (`certify`, `gemm_shapes`)
             └─ NO  → analyze_rocprof.py (system trace) first, to classify
```

**Always start with the system trace** for a new problem unless you already
have strong evidence the bottleneck is kernel-local or library-selection.
`torch.profiler` is the reason ROCm doesn't need its own framework-altitude
profiler kind — it runs on ROCm as-is and covers that altitude directly.

## Quick comparison

| Tool | Overhead | Best at | Worst at |
|:--|:--|:--|:--|
| torch profiler | low | Python-side op hotspots; `certify` for library selection; `gemm_shapes` for AITER-tuning input | real GPU idle-gap / overlap analysis |
| `analyze_rocprof.py` (system trace) | low | bottleneck classification, idle gaps, HIP graph coverage, `families` (AITER/CK/hipBLASLt/Triton/torch-native split) | kernel-internal metrics |
| `counters.py` (PMC) | moderate | one specific hardware ceiling for a known-hot kernel | broad system-level diagnosis |
| `compute.py` (rocprof-compute) | **high** | full Speed-of-Light + empirical roofline for a targeted kernel | profiling a whole run — same overhead class as `ncu` |
| `att.py` (thread trace) | **very high** | per-instruction stall attribution inside one kernel, one CU | anything broader than that one kernel |
| `kernel_bench.py` (paired A/B) | low (untraced) | "did this change help," same-session, drift-controlled | attribution — it verifies, it doesn't diagnose |

## Capture recipes for vLLM / SGLang

### 1. Identify the real command and prewarm

Same shape as every other backend's diagnosis step: find the declared server
command and port, then warm it before capturing. On ROCm the warmup also
resolves AITER/Triton JIT and, if enabled, `TunableOp`/AITER online tuning —
none of that belongs inside the captured window, and for HIP-graph decode
this is also where graph capture happens.

```bash
VLLM_ENABLE_V1_MULTIPROCESSING=0 <declared server command> > /tmp/prewarm.log 2>&1 &
PID=$!
for i in $(seq 1 120); do curl -s http://localhost:8077/health 2>/dev/null | grep -q ok && break; sleep 2; done
curl -s -X POST http://localhost:8077/v1/completions -H "Content-Type: application/json" \
  -d '{"prompt":"warmup","max_tokens":4,"temperature":0}' --max-time 300
kill $PID 2>/dev/null; wait $PID 2>/dev/null; sleep 2
```

`VLLM_ENABLE_V1_MULTIPROCESSING=0` keeps vLLM's engine in the server process
instead of forking a separate `EngineCore` — required for step 2's clean-exit
requirement. SGLang: use its equivalent single-process/non-multiprocessing
flag if one exists for the version in use.

### 2. Capture the system timeline under representative load

**`rocprofv3` only flushes its trace buffers to disk on a clean process
exit.** Killing a wrapped server (SIGTERM/SIGKILL) loses the trace even
though the run otherwise looked fine, because vLLM's engine-core subprocess
(under V1 multiprocessing) and SGLang's per-TP-rank workers do not always
exit cleanly on an external kill of the parent — confirmed end to end on
this repo's MI210 cluster: the earlier "zero trace files" failure mode was
exactly this, not a rocprofv3 bug.

**Preferred: an offline single-process script.** Drive the workload through
the framework's own Python API in one process that exits on its own once the
run finishes (e.g. `vllm.LLM(...).generate(...)`), rather than wrapping the
long-running HTTP server. No forked worker, no signal-timing games:

```bash
VLLM_ENABLE_V1_MULTIPROCESSING=0 \
rocprofv3 --kernel-trace --memory-copy-trace --stats --output-format csv \
  -d /tmp/rocprof_profile -o capture \
  -- python offline_generate.py --model <model> --num-prompts 32 --max-tokens 128
```

Do one untraced warmup `generate()` call first inside the script (a couple of
prompts, few tokens) — in HIP-graph mode this is where graph capture
happens, so the traced region only contains steady-state replay kernels, not
one-time capture kernels — then call `generate()` again over the real batch
inside the traced region.

**If a server must be captured**, shut it down through its own graceful path
(never a hard kill of the process tree) and give the exit *and* the flush a
generous bounded wait — the post-exit flush to disk can itself take minutes
on a large capture, scaling with how much was traced (see the
`--hip-runtime-trace` note below), not with how long the run took:

```bash
VLLM_ENABLE_V1_MULTIPROCESSING=0 rocprofv3 --kernel-trace --memory-copy-trace --stats --output-format csv \
  -d /tmp/rocprof_profile -o capture \
  -- <declared server command> &
ROCPROF_PID=$!
# ... drive the representative benchmark load against the server ...
kill -INT $ROCPROF_PID 2>/dev/null
for i in $(seq 1 60); do kill -0 $ROCPROF_PID 2>/dev/null || break; sleep 5; done
wait $ROCPROF_PID 2>/dev/null
```

Either way: use `--output-format csv` only — `json` duplicates every CSV's
content inside one JSON blob and reached 1.1-1.7GB on a 32-prompt/128-token
Qwen3.5-9B capture for zero extra information (same schema, just wrapped).
Add `--hip-runtime-trace` only when `cpu_overhead`/`graphs`/launch-bound
analysis is actually needed: it traces one HIP runtime API call per kernel
launch/memcpy/event, so on a batched-decode workload it measured 2-4x the
size of the paired kernel trace (233-381MB / 2.5-4M rows on the Qwen3.5-9B
capture above) — don't enable it by default on a large capture. As with
`nsys`, constrain the window to representative load — a multi-minute full
trace is unreadable.

### 3. Mind the process tree

vLLM v1 runs the engine core in a process separate from the API server, and
tensor-parallel workers add one process per GPU; SGLang similarly forks one
worker per TP rank. Each such process loads its own HIP runtime context and
issues its own kernels.

- **Profile the process that actually issues HIP kernels**, not the
  API-server/driver process. A driver-only capture shows near-zero GPU
  activity by construction — that reads as `host_idle`, but it means "wrong
  process," not "the GPU is idle."
- For TP > 1, capture each worker independently; a single capture on the
  driver process will not see the other ranks' kernels.
- **ROCm 7.x's `rocprofv3` can attach to an already-running process**
  (`--attach PID`, verified present in the 7.x CLI, absent from 6.4.1). Where
  available, this is the cleaner way to profile a specific already-spawned
  engine-core or TP-worker process without relaunching the whole server tree
  under the profiler wrapper. On ROCm 6.x, without `--attach`, wrap the
  top-level launch command instead.
- Before trusting an `idle_gaps` or `host_idle` read, confirm with
  `analyze_rocprof.py kernels` that the captured process's trace actually
  contains model kernels.

### 4. Check HIP graph coverage

If decode is meant to run under a captured HIP graph
([`floor.md`](floor.md#3-hip-graphs)), `analyze_rocprof.py graphs` should
show a small number of large graph-launch entries covering the decode steps.
Seeing one entry per op instead means graph capture isn't engaging — that's
a `cpu_launch_bound`/`python_overhead_bound` finding, not a kernel problem.

### 5. Read the system trace

```
python rocprof_profiler/analyze_rocprof.py summary   <trace>   # overview first
python rocprof_profiler/analyze_rocprof.py files      <trace>   # what was actually captured
python rocprof_profiler/analyze_rocprof.py kernels    <trace>   # ranked kernel time — the Amdahl-dominant op
python rocprof_profiler/analyze_rocprof.py families   <trace>   # AITER / CK / hipBLASLt / Triton / torch-native split
python rocprof_profiler/analyze_rocprof.py idle_gaps  <trace>
python rocprof_profiler/analyze_rocprof.py cpu_overhead <trace>
python rocprof_profiler/analyze_rocprof.py memory     <trace>
python rocprof_profiler/analyze_rocprof.py graphs     <trace>
python rocprof_profiler/analyze_rocprof.py host_idle  <trace>
```

`families` is the single most useful subcommand for the characteristic ROCm
finding below: if it shows a fallback share where a fused-library share was
expected, go straight to [`aiter-engagement.md`](aiter-engagement.md) before
touching anything else — a silent fallback masquerades as a hardware or
tuning problem.

### 6. Descend only where the system trace points

- One kernel dominates and needs hardware-ceiling context → `counters.py`
  (≤4 counters per pass — see Pitfalls) or `compute.py`, targeted at that
  kernel only, never a whole run.
- Need per-instruction stall evidence inside that kernel → `att.py`, one CU,
  only after counters already point at a specific stall class.
- Need to confirm a change actually helped → `kernel_bench.py`'s paired,
  interleaved A/B (see [`measurement-protocol.md`](measurement-protocol.md)).

## The characteristic ROCm finding

A kernel that is present and correct but markedly slower than its NVIDIA
counterpart usually means a fallback path was taken — the specific attention
variant or quantization scheme isn't covered by AITER/CK on this ROCm version
and silently landed on Triton or SDPA. Confirm which kernel actually ran
(`families`, or `torch_profiler certify`) before concluding anything about
relative hardware performance. See [`aiter-engagement.md`](aiter-engagement.md).

## Pitfalls

- **Fallback disguised as a hardware limit.** See above —
  [`aiter-engagement.md`](aiter-engagement.md) is the fix, not kernel tuning.
- **`torch.profiler` post-capture hang (measured on this repo's MI210
  bundle).** ROCm 6.4 + torch 2.9.1: after a `torch.profiler` (roctracer)
  session ends, a subsequent async event wait can hang or fault the GPU.
  Drain in-flight work and synchronize before ending the capture — see
  [`measurement-protocol.md`](measurement-protocol.md).
- **PMC counter list size.** Keep each `rocprofv3` counter job to a single
  hardware pass (roughly ≤4 counters). A job that forces multi-pass
  collection has been reported to trigger a GPU hang on CDNA hardware —
  split into multiple single-pass jobs (`counters.py plan` does this for
  you) instead of one large counter list.
- **`rocprof-compute`'s dependency gate.** It refuses to run unless its own
  Python dependency set (`dash`, `textual`, etc.) is importable in whichever
  interpreter runs it — an all-or-nothing preflight, not a partial
  degradation. Run `compute.py doctor` before spending a capture on it,
  rather than discovering the gate mid-run. (Verified clear on this repo's
  MI210 test cluster — system `python3`'s `pandas` was already `2.2.2`,
  ahead of the `>=3` version known to break `rocprof-compute`'s CSV
  converter — but check `doctor` on any other host before relying on this.)
- **`rocprof-compute profile -k` matches a literal substring, and a bad
  filter fails silently, then confusingly — verified end to end.** torch
  GEMMs dispatch through rocBLAS/hipBLASLt as Tensile kernels named like
  `Cijk_Ailk_Bljk_BBS_BH_..._MT256x128x32_...`, which does not contain the
  substring `"gemm"` — `-k gemm` matches zero dispatches. Every
  counter-collection pass then comes back with "0 contexts collected" and a
  header-only CSV, and rocprof-compute's own post-processing crashes later
  with a confusing `KeyError: 'Grid_Size'` deep in `join_prof()`, nowhere
  near the actual cause. List real kernel names first (`rocprofv3
  --kernel-trace --stats`) before writing a filter; `compute.py profile` now
  detects this pattern and fails with that fix instead of forwarding the raw
  traceback. Also verified: rocprof-compute 3.1.0's `profile` still shells
  out to the **deprecated legacy `rocprof`** (v1/v2) internally, not
  `rocprofv3` — every pass logs ROCm's own deprecation warning for it — so
  `doctor` checks for legacy `rocprof`, not `rocprofv3`.
- **`rocprof-compute profile -p <dir>` writes straight into `<dir>`, no
  `<gpu>` subdirectory.** Verified on a real run: with an explicit `-p`, CSVs
  (`pmc_perf.csv`, `pmc_kernel_top.csv`, `roofline.csv`, ...) land directly in
  that directory, alongside rocprof-compute's own internal `perfmon/`
  subdirectory — there is no extra per-GPU subdirectory layer to glob for.
  `compute.py analyze <workload_dir>` expects that directory itself, not a
  parent to search.
- **ATT is version-gated and needs an extra package — verified working end
  to end.** ROCm 6.4.1's `rocprofv3` has **no** `--att`/thread-trace flag at
  all; the option first appears in ROCm 7.1.0/7.2.0. Even there, it fails
  immediately with `rocprof-trace-decoder library path not found` unless the
  `rocprof-trace-decoder` shared library is installed separately — it is a
  standalone GitHub release (`ROCm/rocprof-trace-decoder`), not part of a
  stock ROCm module tree. It installs in user space with no build step and
  no root: the release tarball (e.g. `rocprof-trace-decoder-manylinux-2.28-
  0.1.6-Linux.tar.gz`) contains exactly one file,
  `opt/rocm/lib/librocprof-trace-decoder.so` (~200KB) — extract it anywhere
  and pass its containing directory to `rocprofv3 --att-library-path <dir>`
  (verified on ROCm 7.2.0/MI210; `att.py plan` prints this flag once you know
  the path). All ATT options are plain `rocprofv3` CLI flags — there is no
  separate `-i <job.yaml>` config path for ATT. `--att-buffer-size` takes a
  **plain decimal integer byte count** only (e.g. `67108864`); a unit-suffixed
  string like `64MB` fails with `ValueError: invalid literal for int()`.
  `--att-simd-select`/`--att-shader-engine-mask` accept hex (`0xf`) or
  decimal, both verified — the buffer-size restriction does not generalize to
  every numeric ATT flag. Run `att.py plan` early; if the decoder still can't
  be installed in the time available, treat a decoder-path error as "ATT
  unavailable here," not a usage bug, and fall back to `counters.py`/
  `compute.py` for hardware-ceiling evidence instead. A kernel can match
  `--kernel-include-regex` and still produce no decoded `code.json` (e.g.
  degenerate fill kernels) even though `rocprofv3` exits 0 — that's a decoder
  limitation for that kernel, not a broken capture.
- **`rocprofv3`'s PMC output directory naming is not verbatim.** Regardless
  of the `-d`/output-directory name you pass, each counter-collection pass
  writes into a `pmc_1/<hostname>/<pid>_*` subtree — the `pmc_1` segment
  names "this run's first (and only) counter pass," not your pass number.
  Give each pass its own **base** output directory rather than relying on a
  per-pass filename to disambiguate.
- **A container's ROCm build can differ from the host's, and may be missing
  `rocprof-compute` entirely.** A vLLM ROCm image observed on this cluster
  ships ROCm 7.2.3 and a different torch build than the host venv's ROCm
  6.4.1 / torch 2.9.1; `rocprofv3` was present inside the container but
  `rocprof-compute` was not installed there at all. Check what's actually
  inside the server's container before planning a kernel-internal capture,
  and match the tool's ROCm build to the traced binary's — see the note
  about ABI matching in the capture recipes above.
- **`compute.py`/`att.py` overhead.** Same warning as `ncu`: target one
  kernel, never a whole run. Overhead scales from moderate (PMC) to very high
  (thread trace). For a small workload, measured PMC overhead was under 15%
  of an untraced baseline (a ~40-dispatch, ≤4-counters-per-pass microbench);
  don't extrapolate that figure to a real serving workload's much larger
  kernel and replay count.
- **HIP graph replay vs. eager decode.** Don't sum per-op eager timings and
  call the result graph-era throughput — time whole-graph replay separately
  and use an eager, same-shape run only for sub-forward attribution. Same
  caution the portable contract states for CUDA graphs, same shape here.
- **Collectives show as RCCL, not NCCL.** The topology reasoning differs —
  Infinity Fabric, not NVLink — see [`floor.md`](floor.md) and
  [`algorithms/parallelism.md`](../../algorithms/parallelism.md).

## See also

- [`measurement-protocol.md`](measurement-protocol.md) — warmup, repeats, clocks, the A/B discipline these tools feed
- [`counter-triage.md`](counter-triage.md) — turning a counter capture into a bottleneck verdict
- [`roofline.md`](roofline.md) — per-arch peaks, ridges, and the measured MI210 practical ceiling
- [`aiter-engagement.md`](aiter-engagement.md) — proving which kernel library actually ran
- [`floor.md`](floor.md) — the optimization floor these findings route into
- [`aiter.md`](aiter.md) — the kernel-library stack itself
- [`hardware.md`](hardware.md) — bandwidth and precision by SKU
- [`tooling/profiler.md`](../../tooling/profiler.md) — the portable profiling discipline
