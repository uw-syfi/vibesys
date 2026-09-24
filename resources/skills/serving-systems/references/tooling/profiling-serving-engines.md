# Profiling serving engines: index

How to fill the generic capture tools' lifecycle arguments (`command`,
`env`, `cwd`, `ready_command`, `load_command`, `stop_signal`, `grace_s`,
`timeout_s`, plus each tool's scope args) for a specific serving engine.
The capture tools themselves (`profile_timeline`, `profile_counters`,
`profile_kernel_deep`, `profile_instructions`, `profile_ops`) are
engine-agnostic; engine-specific quirks live in one file per engine under
[`engines/`](../engines/), linked below. This index only covers what's
common across engines: the shared problem and what each argument is for.
Prompts and tools should link here, not to a specific engine's file: read
this first, then follow the link for the engine actually in play.

## Prerequisites

Read [`tooling/profiler.md`](profiler.md) for the portable profiling
discipline (classify with a system timeline before descending) and the
selected backend's `platforms/<backend>/profiler.md` for the platform
mechanics (which tool, what output format, what overhead). This file sits
underneath both: it is what to put *inside* the tool's `command`/`env`
arguments for a given engine, not which tool to pick.

## The core problem: profilers that only flush on clean exit

Some capture tools (rocprofv3's system trace, confirmed; check others before
assuming the same) only flush trace buffers to disk on a clean process exit.
A serving engine that runs its request-handling loop in a forked worker
subprocess can leave that subprocess in a state that does not exit cleanly
when its parent is killed, so a capture that wraps the long-running HTTP
server and then kills it can complete with exit code 0 and zero trace files,
even though the run otherwise looked fine. This is a process-topology
problem, not a bug in the wrapped profiler: the fix is either to give the
process a graceful shutdown path with enough grace time to exit and flush,
or, more reliably, to point the capture at a single-process, non-serving
entry point that exits on its own once the workload finishes.

## Filling the generic tool arguments

| Argument | Meaning | Typical fill for an offline single-process capture | Typical fill for a server capture |
|:--|:--|:--|:--|
| `command` | what to launch under the capture tool | an offline script driving the engine's Python API directly | the engine's server launch command |
| `env` | environment variables the launch needs | the engine's single-process flag (see the engine's file) | same |
| `ready_command` | how to tell the server is up | not needed (not a server) | a health/models endpoint poll |
| `load_command` | how to drive the traced work | not needed (the script drives its own load) | the benchmark command against the server |
| `stop_signal` | how to end the capture | not needed (the process exits on its own) | a graceful signal (e.g. `SIGINT`), never a hard kill |
| `grace_s` | how long to wait for exit + flush before giving up | not needed | generous (minutes; scales with trace volume, not run duration) |

An offline single-process capture is preferred wherever the engine supports
it: it sidesteps the clean-exit problem above entirely instead of relying on
a graceful-shutdown path that may not be verified. See the engine's own file
for whether that path exists and how to reach it.

## Per-engine files

| Engine | File | Status |
|:--|:--|:--|
| vLLM | [`engines/vllm-profiling.md`](../engines/vllm-profiling.md) | Verified (offline single-process capture, torch.profiler interface, post-capture hang) |
| SGLang | not yet written | No verified capture facts yet; add `engines/sglang-profiling.md` following the vLLM file's shape once some exist |

## See also

- [`tooling/profiler.md`](profiler.md): the portable profiling discipline
- [`platforms/`](../platforms/): the selected backend's capture mechanics and output formats
- [`engines/`](../engines/): source-code maps for these engines (not profiling-specific)
