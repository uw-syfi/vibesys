# Profiling on ROCm

Which `rocprof`-family tool answers which question, at which altitude, and
how to capture one cleanly against a running vLLM or SGLang server on CDNA.
The discipline is the portable one in [`tooling/profiler.md`](../../tooling/profiler.md)
(classify with a system timeline before descending); this file is the tool
substitution table plus the ROCm-specific mechanics.

> **Status.** The `rocprof` profiler kind is wired up: system trace, PMC
> counters, thread trace, kernel-internal, and paired A/B timing all have a
> workspace tool (`rocprof_profiler/`, below). Command syntax is accurate to
> `rocprofv3` / `rocprof-compute`'s documented CLI; end-to-end capture against
> a live vLLM/SGLang server has not yet been verified on this repo's MI210
> test machine — verify flags against your ROCm version. The `torch.profiler`
> pitfall in this file's Pitfalls section **is** measured on this repo's
> MI210 bundle.

## Altitudes and tools

| Altitude | Answers | Tool | Where |
|:--|:--|:--|:--|
| System timeline | host/device overlap, idle gaps, launch cost, HIP graph coverage | `rocprofv3` system trace | `rocprof_profiler/analyze_rocprof.py` |
| Framework / op | which torch op dominates; which library a dispatch actually hit | `torch.profiler` (works unmodified on ROCm) | `resources/profilers/torch/analyze_torch_profile.py` — `certify`, `gemm_shapes`, `roofline` |
| Counters (PMC) | which hardware ceiling one kernel is against | `rocprofv3` hardware counters | `rocprof_profiler/counters.py` — `plan`, `report`, `triage` |
| Kernel-internal | full Speed-of-Light + memory chart + empirical roofline for one kernel | `rocprof-compute` (named `omniperf` before ROCm 6.3) | `rocprof_profiler/compute.py` — `doctor`, `profile`, `analyze` |
| Instruction-level | per-instruction stalls inside one kernel, on one CU | `rocprofv3` thread trace (ATT) | `rocprof_profiler/att.py` — `plan`, `hotspots` |
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
none of that belongs inside the captured window.

```bash
<declared server command> > /tmp/prewarm.log 2>&1 &
PID=$!
for i in $(seq 1 120); do curl -s http://localhost:8077/health 2>/dev/null | grep -q ok && break; sleep 2; done
curl -s -X POST http://localhost:8077/v1/completions -H "Content-Type: application/json" \
  -d '{"prompt":"warmup","max_tokens":4,"temperature":0}' --max-time 300
kill $PID 2>/dev/null; wait $PID 2>/dev/null; sleep 2
```

### 2. Capture the system timeline under representative load

Wrap the server command with `analyze_rocprof.py`'s capture mode, drive a
representative benchmark load, then stop. As with `nsys`, constrain the
window — a multi-minute full trace is unreadable.

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
  rather than discovering the gate mid-run.
- **`compute.py`/`att.py` overhead.** Same warning as `ncu`: target one
  kernel, never a whole run. Overhead scales from moderate (PMC) to very high
  (thread trace).
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
