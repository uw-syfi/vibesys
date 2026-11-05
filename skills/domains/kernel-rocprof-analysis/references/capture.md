# Capturing on ROCm

How to run `capture.py` as a profiling job, what each recipe costs, how to keep startup out of the
capture, and how to keep the profiler, the HIP runtime and the driver consistent.

## Contents

- [Recipes](#recipes)
- [Two kinds of capture job](#two-kinds-of-capture-job)
- [Capture windows](#capture-windows)
- [What a capture leaves behind](#what-a-capture-leaves-behind)
- [Version consistency](#version-consistency)
- [When a capture comes back empty](#when-a-capture-comes-back-empty)

## Recipes

Every recipe takes `--name` (the capture's directory, default the recipe name; use one name per
capture when a job takes several), `--profiler-arg <arg>` (repeatable, passed to the profiler
before its output flag) and `--dry-run` (print the record and the profiler command, run nothing).
End the options with `--`; everything after it is the command to profile.

| Recipe | Runs | Use it to | Cost |
| --- | --- | --- | --- |
| `timeline` | `rocprofv3 --kernel-trace --memory-copy-trace --stats`, CSV | see where GPU time goes, idle gaps, launch overhead | one run; small per-dispatch overhead. `--hip-api` adds the HIP runtime trace (needed by `trace cpu_overhead` and `graphs`), with per-call overhead that matters for launch-bound work; `--markers` adds roctx ranges |
| `counters` | `rocprofv3 --pmc <names>`, CSV | measure one counter pass on named kernels | one full run per pass; kernels are serialized while counted, so their timing is not the benchmark's |
| `thread-trace` | `rocprofv3 --att` on the kernels matching `--kernel-include` | find the instructions that stall | the heaviest: one compute unit (`--target-cu`), large output (`--buffer-bytes`); ROCm 7.1 or newer and the trace decoder library |
| `kernel-deep` | `rocprof-compute profile -k <substring>` | speed-of-light and roofline tables for one kernel | one replay per counter pass: many times the workload's own run time |
| `check` | nothing | print the versions and whether a capture would be refused | a few seconds |

`--kernel-include <regex>` narrows any rocprofv3 recipe to matching kernels; take the regex from a
timeline's kernel names. `counters` takes counter names, not set names: print them with
`analyze.py counters plan --arch <gfx> --sets <sets>`, and keep each pass at the size `plan`
prints, since packing too many counters into one pass has hung GPUs. The thread-trace decoder is
found through `$ROCPROF_ATT_LIBRARY_PATH` or `$ROCM_PATH/lib`; install it in the candidate image.

## Two kinds of capture job

Both go through `vibesys-cli` (the `vibesys:measure` skill says how to submit and wait). `--wrap`
is an argv prefix the CLI puts in front of a command without reading it; `capture.py` and its
options, ending with `--`, are that prefix.

**A standard capture** profiles the server under the task's own profile load: the task's `[profile]`
command, the same for every candidate, so two captures of two commits are comparable.

```bash
vibesys-cli submit profile <sha> --wrap 'python3 tools/capture.py timeline --hip-api --'
```

The CLI starts the task's server command behind the prefix, waits until it is ready, runs the
profile load, then stops the server with SIGTERM and waits before it kills it, so the profiler can
flush. It never caches or scores the job, and copies the job's artifacts back. Use it first, and
for every capture you will compare with another.

**A free-form capture** profiles a load you choose, in the candidate container: one request shape,
one batch size, a microbenchmark of one kernel. With `--service`, the prefix goes in front of the
service; without it, in front of the command.

```bash
# your own load against the server, which runs under the profiler
vibesys-cli submit command <sha> --wrap 'python3 tools/capture.py counters --pmc TCC_HIT_sum TCC_MISS_sum --kernel-include Cijk_ --' \
  --service '<server argv>' --probe '<readiness argv>' -- <load argv>
# a kernel microbenchmark, no server
vibesys-cli submit command <sha> --wrap 'python3 tools/capture.py thread-trace --kernel-include fmha --' -- python bench/attn.py
# versions only: cheap, run it after any image change
vibesys-cli submit command <sha> -- python3 tools/capture.py check
```

A free-form capture answers a narrower question faster and cheaper, but its numbers describe your
load, not the task's; say which load you used when you report it.

## Capture windows

A server capture includes startup (weight load, warmup, KV-cache initialization), which can be most
of the trace. Keep it out at capture time where you can; `analyze.py trace ... --window
<start_s>:<end_s>` slices a timeline afterwards, in seconds from its first dispatch.

- **A delay**: `--collection-period <delay_s>:<duration_s>` collects only from `delay_s` seconds
  after the process starts, for `duration_s` seconds. It needs no engine change, but you must know
  how long startup takes; read it from an earlier timeline or the job's log.
- **roctx-driven regions**: `--selected-regions` collects only between `roctxProfilerResume` and
  `roctxProfilerPause` calls in the profiled program, so the engine opens the window itself once
  it is warm (the `serving-systems` skill's engine profiling conventions say how). It follows the
  load exactly, at the cost of an engine change. Check that the image's `rocprofv3 --help` lists
  the flag.

## What a capture leaves behind

`capture.py` writes into `$VIBESYS_ARTIFACTS_DIR/<name>/` (or `--output-dir`): `capture.json`
first, then the profiler's own output, one `<host>/<pid>/` directory per traced process. The
analysis tools find files at any depth and never read `capture.json` as trace data. `capture.json`
holds the recipe, its options, the wrapped command, the exact profiler command line, the versions
found, the preflight findings (and whether they were overridden) and the start time; quote it when
you report a capture. `capture.py` refuses a name that already holds a capture.

The wrapper `exec`s the profiler: the profiler takes over the wrapper's process, so the job's
SIGTERM reaches it directly. rocprofv3 writes its trace when the traced program exits normally, so
a program that dies by SIGKILL, or exits through `os._exit`, leaves no trace for its process.

## Version consistency

Three versions decide whether a capture works:

| Version | Owner | Read from |
| --- | --- | --- |
| the profiler's ROCm release | the candidate image (yours) | `rocprofv3 --version` (`rocm_version:`) |
| the HIP runtime the program loads | the candidate image (yours) | `hipconfig --version`; `torch.version.hip` when the engine uses torch, whose wheels carry their own HIP runtime |
| the amdgpu driver | the node (not yours) | `/sys/module/amdgpu/version`, else `amd-smi version` |

`capture.py` refuses a rocprofv3 from another ROCm major release than a HIP runtime it found, or
from an older release than that runtime: its tracing tables do not know the runtime's newer ones,
and the trace comes back empty or the program crashes. A profiler newer within the same major
release is allowed. Fix the image instead of passing `--allow-version-mismatch`:

- install the profiler from the same ROCm release as the runtime (in a ROCm base image, the
  `rocprofiler-sdk` package of that release), or
- install the torch build for the image's ROCm release, so `torch.version.hip` and `/opt/rocm`
  agree.

The driver is recorded, never judged by `capture.py`: if the image's runtime cannot run on the
node's driver, the job itself fails, and the CLI's error names both versions. Then move the image to
a ROCm release that supports the node's driver; `capture.json` and `capture.py check` show what the
node has.

## When a capture comes back empty

- **No trace files at all.** The program did not exit normally: a worker killed by its parent, a
  server that ignores SIGTERM, or `os._exit`. See the `serving-systems` skill's engine profiling
  conventions for clean shutdown, or capture an offline single-process run instead.
- **Trace files but no kernels from the load.** `analyze.py trace host_idle` says so. The window
  missed the load (delay too long or too short, regions never resumed), or the work ran in a child
  process that did not inherit the environment the profiler set.
- **A counter pass with zero rows.** The `--kernel-include` regex matched nothing; take the name
  from a timeline. rocprof-compute's `-k` is a literal substring with the same pitfall.
- **The program crashes only under the profiler.** Run `capture.py check`: a version mismatch is
  the usual cause.
