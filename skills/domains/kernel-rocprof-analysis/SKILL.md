---
name: kernel-rocprof-analysis
description: Capture and interpret AMD rocprofv3 and rocprof-compute evidence for a ROCm kernel or serving task. Ships a capture wrapper that checks the profiler, HIP runtime and driver versions before it runs, and local tools that analyze copied-back traces, counters, thread traces, torch traces and A/B microbenchmarks. Use when a ROCm workload needs a profile, a capture failed or came back empty, or a measured bottleneck needs diagnosis.
---

# Kernel rocprof analysis

A profile explains a number; it never replaces one. Use the task's benchmark for every before and
after decision, and a profile to choose what to try next.

## The two scripts

Both are in this skill's `scripts/` directory; how to find it depends on your harness:
read `references/harness/<harness>.md` for the harness you are running in.

- `capture.py` runs inside the candidate's container, as the argv prefix of a profiling job. It
  checks that the profiler can trace what the job runs, records what it found in `capture.json`,
  then replaces itself with the profiler. It needs only `python3` (3.8 or newer). Copy it into the
  candidate repository (for example as `tools/capture.py`) and commit it: a job runs a commit, in
  the candidate's own image.
- `analyze.py` runs on your machine, on the files a job copied back. It needs Python 3.10 or newer
  and nothing else: no GPU, no ROCm. `analyze.py <group> --help` lists each group's commands.

| Group | Reads | Answers |
| --- | --- | --- |
| `trace` | a timeline capture directory | top kernels, kernel library families, GPU idle gaps, HIP launch overhead, memory copies, HIP graphs, whether the load was captured at all (`host_idle`), SQL on a rocpd export (`query`), all of it (`summary`) |
| `counters` | PMC pass directories | counter sets per architecture (`list-sets`, `plan`), per-kernel derived metrics (`report`), a bottleneck verdict per kernel (`triage`) |
| `att` | a decoded thread-trace dispatch directory | the instructions and source lines that stall (`hotspots`) |
| `compute` | a rocprof-compute workload directory | speed-of-light and roofline tables (`analyze`); the raw CSVs when `rocprof-compute` is not installed here |
| `compare` | two captures of one kind | per-family and per-kernel time deltas, or per-kernel counter deltas, `b - a` |
| `torch` | a `torch.profiler` trace | whether the trace can be trusted (`certify`), GEMM shapes by GPU time, per-op roofline |
| `bench` | microbenchmark logs and sample files | samples from a driver log, unpaired and paired A/B verdicts |

## Capture in three steps

Each step costs more and perturbs timing more than the one before, so take the next step only when
the previous one named a target. Recipes, job forms, costs and capture windows are in
[capture.md](references/capture.md).

1. **Timeline.** `capture.py timeline` records every kernel dispatch and memory copy. Read it with
   `analyze.py trace summary`: confirm the load was captured (`host_idle`), then take the dominant
   kernels' exact names from `kernels`. Library and JIT kernel names rarely match an operator name
   (a GEMM is `Cijk_...`, not `gemm`), so never guess a filter.
2. **Counters**, on the kernels the timeline named. `analyze.py counters plan --arch <gfx> --sets
   <sets> --kernel <regex>` prints the counter names of each pass; each pass is one
   `capture.py counters --pmc <names> --kernel-include <regex>` job, because every pass reruns the
   workload. Merge the passes with `analyze.py counters triage <dirs> --arch <gfx>`.
3. **Thread trace**, on one kernel, only after counters point at a stall class:
   `capture.py thread-trace --kernel-include <regex>`, then `analyze.py att hotspots <dir>`. It needs
   rocprofv3 from ROCm 7.1 or newer and the separate rocprof-trace-decoder library in the image.

`capture.py kernel-deep --kernel <substring>` runs `rocprof-compute profile` instead of step 2 when
you want its speed-of-light tables; it replays the workload once per counter pass, so expect a run
many times longer than the workload.

## Versions must match

The candidate image is yours: you choose its ROCm userspace, its torch and its profiler. The amdgpu
driver is the node's and you cannot change it. `capture.py` refuses a capture whose rocprofv3 comes
from a different ROCm major release than the HIP runtime the job loads, or from an older release,
and names both versions; torch wheels carry their own HIP runtime, so `torch.version.hip` counts,
not only `/opt/rocm`. Fix the image rather than overriding the check; how is in
[capture.md](references/capture.md#version-consistency). `capture.py check` runs only the check.

## Interpret

Match each dispatch to the benchmarked candidate, input shape, dtype, device and warmup state;
exclude first-call JIT compilation and tuning from the captured window.

Form one hypothesis from the relevant counters. Occupancy on CDNA follows wavefronts of 64 lanes and
is bounded by VGPR, AGPR, LDS and scratch use; low occupancy alone does not prove it limits
throughput. HBM bandwidth and L2 hit rates need byte counts and access patterns to be meaningful.
For matrix-heavy kernels, compare the MFMA issue rate with the architecture's peak before blaming
memory. LDS bank conflicts matter only when LDS traffic is on the critical path. `compare` shows
what changed between two captures; it does not show that the change made the candidate faster.

## Report

Give the capture identity (its `capture.json`: recipe, versions, the command), the dominant
dispatches, the counter values, what was unavailable or uncertain, and the next experiment. Confirm
any optimization with the accuracy checker and the scored benchmark.
