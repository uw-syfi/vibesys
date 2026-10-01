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

## Add the profiler prompt

Add `<kind>.j2` under each strategy that uses the profiler, currently
`src/vibesys/orchestration/{multi,evolve}/prompts/profilers/` (`profile-guided-multi-agent`
reuses `multi`'s prompts; it is a preset of `multi`, not a separate strategy
folder). Explain how
that strategy's agent should collect and interpret evidence, which limitations
it must report, and how it should produce `ProfilerSummary`. Review each
strategy's prompt separately; there is no cross-strategy fallback.

## Validate the implementation

Test domain and interface compatibility, registry-derived packaging names, MCP tool
registration, capability and failure paths, artifact metadata, and prompt selection. Add
platform-gated integration coverage when collection depends on host tooling.

A conventional profiler must not require new context fields, sandbox mount branches, CLI
flags, or MCP/prompt dispatch mappings. If it does, first determine whether the requirement
is a reusable framework capability or a profiler-local implementation detail.
