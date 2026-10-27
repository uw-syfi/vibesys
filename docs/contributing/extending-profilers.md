# Extending Profilers

VibeSys profiler implementations expose evidence-gathering tools to the profiler agent.
The framework selects a profiler, copies its bundled support package into the project,
starts its MCP server, renders its prompt, and records the returned `ProfilerSummary`.

## Declare a profiler

Add a uniform identifier to `ProfilerKind` and a `ProfilerDefinition` to the typed registry
in `vibesys.orchestration.profilers`. Definitions contain behavioral policy that cannot be inferred,
such as supported domains, backend restrictions, or interface constraints. Keep environment and
platform `auto` selection in `resolve_profiler_kind` rather than the packaging definition.

The identifier is used without transformation. For a kind named `perf`, VibeSys derives:

| Resource | Derived name |
| --- | --- |
| Project support directory | `perf_profiler/` |
| MCP entrypoint | `perf_profiler/server.py` |
| MCP server name | `vibesys-perf-profiler` |
| Agent prompt | `profilers/perf.j2` |

Filesystem and Python identifiers retain underscores. MCP server identifiers normalize
underscores to dashes, so `macos_cpu` produces `vibesys-macos-cpu-profiler`.

`auto` and `none` are modes, not runnable profiler definitions.

## Implement the support package

Create `resources/profilers/<kind>/server.py`. The MCP server should expose tools for
capability detection, diagnostic collection, and useful report analysis. Tool results must
use structured diagnostics for unavailable tools, permissions, or unsupported facilities.

Profiling must remain separate from the trusted scored benchmark. Persist raw artifacts
and reproduction metadata rather than embedding unbounded profiler output in the agent
response. Target the process that performs the workload, including child workers when
necessary.

NCU is the `auto` default for kernel-writing on the CUDA backend when the run
environment supports it. `--profiler none` disables it. Explicit `--profiler ncu`
is accepted only for kernel-writing on CUDA. Its bundled MCP server provides
capability discovery and bounded `.ncu-rep` inspection. The profiler agent
captures through its shell in the candidate's execution environment. A session
MCP server runs as a framework child process, so it must not launch candidate
executables outside that environment. Capture artifacts belong in the run's
writable profile artifact directory, and NCU replay timings are diagnostic rather
than scored benchmark results.

FlashInfer is an opt-in alternative to NCU for CUDA kernel-writing
(`--profiler flashinfer`). A kernel records per-block begin and end events with
FlashInfer's `profiler.cuh` macros, which compile to nothing unless
`FLASHINFER_ENABLE_PROFILER` is defined. The profiler agent builds an
instrumented copy and saves the raw `uint64` buffer from its own GPU job; the
bundled MCP server is pure Python and only decodes and summarizes that buffer
(or a Chrome trace-event JSON file) per phase and per block. The
`kernel-timeline-analysis` skill tells implementers where to put probes.

`ncu_flashinfer` offers both in one session. Its definition stages the `ncu`
and `flashinfer` packages as extra support kinds, and its server calls each
package's `register_tools` on one MCP instance, prefixing the timeline tools
with `timeline_` because both packages define `capabilities`. Its prompt reuses
the macros in `profilers/_ncu_sections.j2` and `profilers/_flashinfer_sections.j2`,
which the single-profiler prompts also render.

rocprof is the `auto` default for kernel-writing on the ROCm backend when the
run environment supports it. It uses the same MCP server and prompt as LLM
serving; the kernel-writing domain's profiler guidance and the
`kernel-rocprof-analysis` skill supply the kernel-level focus.

## Add the profiler prompt

Add `<kind>.j2` under `src/vibesys/prompts/shared/profilers/`. `multi` (and
`profile-guided-multi-agent`, a preset of it) renders it through its `profiler.j2`, which includes
`profilers/<kind>.j2` for the run's profiler and adds where to write bounded evidence; `evolve` has
its own `profiler.j2` under `src/vibesys/orchestration/evolve/core_policy/prompts/` that includes the same
file. Explain how the agent should collect and interpret evidence, which limitations it must
report, and how it should produce `ProfilerSummary`. The template receives `profile_focus`,
`benchmark_command`, `modality`, `profile_execution`, `profiler_support_name`,
`profiler_mcp_name`, `objective` and `runtime_notes`.

## Validate the implementation

Test domain and interface compatibility, registry-derived packaging names, MCP tool
registration, capability and failure paths, artifact metadata, and prompt selection. Add
platform-gated integration coverage when collection depends on host tooling.

A conventional profiler must not require new context fields, sandbox mount branches, CLI
flags, or MCP/prompt dispatch mappings. If it does, first determine whether the requirement
is a reusable framework capability or a profiler-local implementation detail.
