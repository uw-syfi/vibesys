# Capturing on ROCm

How to run `capture.py` as a profiling job, what each recipe costs, how to keep startup out of the
capture, and how to keep the profiler, the HIP runtime and the driver consistent.

## Contents

- [Recipes](#recipes)
- [Two kinds of capture job](#two-kinds-of-capture-job)
- [Capture windows](#capture-windows)
- [Complete and bounded captures](#complete-and-bounded-captures)
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

Both go through `vibesys-cli submit command` (the `vibesys:measure` skill says how to submit and
wait), which names the image the command runs in. `--wrap` is an argv prefix the CLI puts in front
of a command without reading it; `capture.py` and its options, ending with `--`, are that prefix.
`capture.py` and the profiler must be in the candidate's image, where the server runs.

**A standard capture** profiles the server under a fixed load from the task's own image, the same
for every candidate, so two captures of two commits are comparable. With `--image harness` on a
task with `[candidate]`, the command runs in the task's image beside the candidate's server, as the
benchmark does, and `--wrap` goes in front of the server. Use the task's own short load when it has
one (its README or harness says, such as a `profile` mode of its benchmark script), and the same
argv for every capture you compare.

```bash
vibesys-cli submit command <sha> --image harness --wrap 'python3 tools/capture.py timeline --hip-api --' -- <the task's load argv>
```

The CLI starts the candidate's server behind the prefix, waits until it is ready, runs the load,
then stops the server with SIGTERM and waits a short stop grace (the operator's setting, 30 seconds
unless changed) before it kills it, so the profiler can flush. A profiler still writing a large
trace when the grace ends is killed mid-write: the job still exits 0, but the trace is cut short
(see [Complete and bounded captures](#complete-and-bounded-captures)). It never caches or scores
the job, and copies the job's artifacts back. Use it first, and for every capture you will compare
with another.

**A free-form capture** profiles a load you choose: one request shape, one batch size, a
microbenchmark of one kernel. Your own load against the server goes in the task's image as above,
with your argv after `--`. A load that runs in the candidate container goes there with `--image
candidate`; with `--service` the prefix goes in front of the service, and without it in front of
the command.

```bash
# your own load against the candidate's server, which runs under the profiler
vibesys-cli submit command <sha> --image harness --wrap 'python3 tools/capture.py counters --pmc TCC_HIT_sum TCC_MISS_sum --kernel-include Cijk_ --' -- <load argv>
# a kernel microbenchmark in the candidate's image, no server
vibesys-cli submit command <sha> --image candidate --wrap 'python3 tools/capture.py thread-trace --kernel-include fmha --' -- python bench/attn.py
# versions only: cheap, run it after any image change
vibesys-cli submit command <sha> --image candidate -- python3 tools/capture.py check
```

On a task without `[candidate]`, everything runs in the task's image: use `--image harness`, and
`--wrap` goes in front of `--service` or of the command, as in the candidate container above.

A free-form capture answers a narrower question faster and cheaper, but its numbers describe your
load, not the task's; say which load you used when you report it.

## Capture windows

A server capture includes startup (weight load, warmup, KV-cache initialization), which can be most
of the trace. Keep it out at capture time where you can; `analyze.py trace ... --window
<start_s>:<end_s>` slices a timeline afterwards, in seconds from its first dispatch.

- **A delay**: `capture.py`'s `--collection-period <delay_s>:<duration_s>` collects only from
  `delay_s` seconds after the process starts, for `duration_s` seconds (it passes rocprofv3
  `--collection-period <delay>:<duration>:1`, one window, in seconds). It needs no engine change, but you must know
  how long startup takes; read it from an earlier timeline or the job's log.
- **roctx-driven regions**: `--selected-regions` collects only between `roctxProfilerResume` and
  `roctxProfilerPause` calls in the profiled program, so the engine opens the window itself once
  it is warm (the `serving-systems` skill's engine profiling conventions say how). It follows the
  load exactly, at the cost of an engine change. Check that the image's `rocprofv3 --help` lists
  the flag.

## Complete and bounded captures

rocprofv3 writes its trace files when the traced program exits. A whole-run CSV kernel trace of a
server under minutes of load holds millions of dispatches and can reach gigabytes, more than the
profiler writes within a short stop grace; it is then cut short, while the small
`*_kernel_stats.csv` written from the same records is complete. Nothing in the job's exit status
says so.

- **Check before you trust a trace.** `analyze.py trace completeness <dir>` prints JSON whose
  `status` is `complete`, `incomplete` or `unchecked` (no stats file to compare with). It compares
  the trace's dispatch count with the stats file's and checks that the trace does not end
  mid-record; `trace summary` and every timeline view print an `INCOMPLETE TRACE` warning. On an
  incomplete trace, `kernels`, `families` and `compare timeline` take whole-run totals from the
  stats file; capture window, GPU busy, idle gaps and host idle describe only the traced start.
- **Take totals from the stats file.** Keep `--stats` (the `timeline` recipe sets it): per-kernel
  calls and time over the whole run come from `*_kernel_stats.csv`, which stays small whatever the
  run length.
- **Bound the timeline.** Trace a window of steady load instead of the whole run:
  `capture.py timeline --collection-period <delay_s>:<duration_s> --`, with a duration of tens of
  seconds, after startup and warmup. Narrow it further with `--kernel-include`. Wrapping rocprofv3
  directly, pass `--collection-period <delay>:<duration>:<repeat>` yourself (seconds unless
  `--collection-period-unit` says otherwise). The flag's form has changed between ROCm releases:
  read the image's `rocprofv3 --help` first, with a cheap `submit command <sha> --image candidate --
  rocprofv3 --help`.
- **Mind the format.** CSV is what `analyze.py` reads, and it spends a line of text on every
  dispatch; JSON is larger still. A bounded window is what keeps a CSV trace small. rocprofv3's
  binary formats (`pftrace` for Perfetto, `otf2`, and `rocpd` from ROCm 7) avoid that text, but
  `analyze.py` reads only a rocpd export, through `trace query`.

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

A capture that comes back cut short rather than empty is in
[Complete and bounded captures](#complete-and-bounded-captures).

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
