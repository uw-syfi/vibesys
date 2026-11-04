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

`command`/`ready_command`/`load_command` may be arbitrary multi-line shell
text (heredocs, embedded quotes, a multi-line `python3 -c "..."`): the
capture tools write each to its own script file before running it, so
nothing re-tokenizes the text on its way to the shell that finally executes
it.

## Avoid `$(...)` command substitution for values the launch needs

Some capture tools inject themselves into every process in the launched
tree, not only the top-level target -- including a small helper subprocess
spawned just to compute a value via `$(...)` command substitution (e.g.
picking a free port). If that injected tool prints anything of its own to
the helper's stdout (a one-time capability/diagnostic line, commonly
emitted the moment its tool library loads into a process, independent of
whether that process ever does any of the work being profiled), the
substitution captures the diagnostic line together with the real value,
and using the result unquoted (`--port $PORT`) word-splits it into extra,
unexpected arguments for the real target. Observed for real: a port-picker
helper's `$(python3 -c "...")` output picked up a stray diagnostic line
from the profiling tool, and the server rejected its own `--port` argument
as a result.

This is not just a quoting problem, and no profiler-side env var or CLI
flag fixes it: on real MI210 hardware, no combination of log-level/quiet
env vars (7 candidates tried, plus `rocprofv3 --log-level fatal`)
suppressed the injected tool's own banner. The only reliable fix is to
never let a value-computing helper run under the profiler in the first
place.

The safe pattern: pick the value in `setup_command` (every capture tool
takes one; it runs to completion *before* `command` starts, entirely
outside the profiler), write it with a plain redirect, then have `command`
read it back with a shell builtin, never a command substitution:

```bash
# setup_command (runs unprofiled, before command):
python3 -c "..." > /tmp/port   # plain redirect, not $(...)

# command (runs under the profiler):
read -r PORT < /tmp/port       # builtin read, not PORT=$(cat /tmp/port)
```

`read -r ... < file` is a shell builtin: it never forks a subprocess, so
there is nothing for the profiling tool to inject into and nothing that can
leak a diagnostic line into the value. `PORT=$(cat /tmp/port)` looks safer
than the original `$(python3 -c "...")` but is not: `cat` is still a forked
process the profiling tool can inject into, so it reintroduces the exact
same risk one level removed. Never use any `$(...)` form for a value a
profiled command needs, including `$(cat ...)`, whether it appears in
`command`, `ready_command`, or `load_command` -- all three run under the
profiler. `setup_command` is the one lifecycle step that does not: use it
for any value-computing step, not just port selection.

## MCP client tool-call timeout

A `profile_*` capture tool call does not return until the whole lifecycle
finishes: for a server capture this can take 10-25 minutes (weight load,
warmup, the benchmark run, then the post-stop flush). Set the MCP client's
own tool-call timeout above the capture's `timeout_s` (`profile_ops`
defaults `timeout_s` to 1800s), not the framework's or model's default
timeout for a "normal" tool call. A client that abandons the call (times
out, or the session disconnects) does stop the capture -- the server
notices the cancellation and tears down the target process tree the same
way a timeout would, so nothing keeps running on the GPU unsupervised --
but the abandoned call still loses the response text, and a second capture
call made too soon after can find the first one's teardown still in
progress. Use `captures()` (which returns immediately even while a capture
is in flight) to check on a capture whose original tool call already timed
out client-side, and expect a "busy: ..." reply, not a queued capture, if
you call a `profile_*` tool again before the prior one's teardown settles:
this server runs one GPU-using capture at a time per process.

## `profile_counters` cost: one target run per packed pass, not per counter set

`profile_counters` packs the requested counter sets into as few rocprofv3
passes as it can (see `counters.py`'s packing model); each *pass*, not each
*requested set*, re-runs the whole profiled workload from scratch. Two sets
that pack into one pass cost one target run together; two sets that don't
pack cost two. The tool's own output reports how many passes were planned
and how many actually ran (a rejected packed pass falls back to one pass
per set, raising the run count above the planned count) -- read that before
assuming a given `sets=[...]` list costs `len(sets)` full workload runs.

## Server captures default to the load-phase window

A server capture also captures that server's own startup (weight load,
warmup, KV init, ...) ahead of the actual benchmarked traffic, which can
dwarf the traffic itself and pollute a system-trace analysis's kernel/
family tables with one-time setup work. The timeline analysis tools default
to the capture's recorded load-phase window (the span between
`ready_command` succeeding and `load_command` finishing) rather than the
whole run, and state which window they used in their output header; pass
`window='all'` to see the whole run, or `window='startup'` to look at the
excluded setup phase on its own.

## Warm targets: repeated windows without relaunching

Every `profile_*` capture tool above launches a fresh process for one
capture's whole lifetime, then tears it down. `start_target(command, ...)`
launches a process once and keeps it running across multiple, separate
profiling windows against it; `profile_ops(target=<id>, load_command=...,
duration_s=...)` takes one such window (`load_command` bounds it: it must
run long enough for real work to happen). Call `stop_target(target)` when
done, or let the MCP server's own exit stop it. Use this when the same
already-warmed-up process needs several before/after windows (e.g.
comparing two request shapes on the same server) instead of paying a fresh
launch (weight load, warmup, KV init) per window; overhead of leaving a
target armed but idle between windows is negligible.

Two limits to know before reaching for it:

- **Torch-level only.** A warm target only supports the torch plugin's
  `profile_ops`, not a system-wide trace or PMC counters: rocprofv3-backed
  capture tools always launch their own target for the capture's lifetime
  (`profiling_capabilities` reports whether this host's rocprofv3 build
  even supports attach at all; VibeSys does not implement attach-based
  capture either way -- passing `target=` to a rocprofv3 tool returns a
  clear error instead of attempting it).
- **`inject=False` for engines that manage their own profiler session.** If
  `command` already opens its own, separate `torch.profiler` session
  in-process (check the engine's file under [`engines/`](../engines/) for
  its actual contract before assuming one), pass `inject=False` to
  `profile_ops`: running the tool's own injected session *and* the
  engine's own session in the same process crashes the CUPTI/roctracer/
  kineto backend outright (a SIGSEGV, not a catchable error) rather than
  raising cleanly. `inject=False` skips arming the tool's own session while
  still exporting `VIBESYS_TORCH_PROFILE_OUT_DIR` so the engine's own
  profiler can write its trace where the discovery/analysis pipeline reads
  from.

## Per-engine files

| Engine | File | Status |
|:--|:--|:--|
| vLLM | [`engines/vllm-profiling.md`](../engines/vllm-profiling.md) | Verified (offline single-process capture, torch.profiler interface, post-capture hang) |
| SGLang | not yet written | No verified capture facts yet; add `engines/sglang-profiling.md` following the vLLM file's shape once some exist |

## See also

- [`tooling/profiler.md`](profiler.md): the portable profiling discipline
- [`platforms/`](../platforms/): the selected backend's capture mechanics and output formats
- [`engines/`](../engines/): source-code maps for these engines (not profiling-specific)
