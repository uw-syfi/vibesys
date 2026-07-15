# Extending Profilers

VibeServe profiler implementations expose evidence-gathering tools to the profiler agent.
The framework selects a profiler, copies its bundled support package into the workspace,
starts its MCP server, renders its prompt, and records the returned `ProfilerSummary`.

## Declare a profiler

Add a uniform identifier to `ProfilerKind` and a `ProfilerDefinition` to the typed registry
in `vibe_serve.profilers`. Definitions contain behavioral policy that cannot be inferred,
such as supported domains or interface constraints. Keep backend, environment, and
platform `auto` selection in `resolve_profiler_kind` rather than the packaging definition.

The identifier is used without transformation. For a kind named `perf`, VibeServe derives:

| Resource | Derived name |
| --- | --- |
| Workspace support directory | `perf_profiler/` |
| MCP entrypoint | `perf_profiler/server.py` |
| MCP server name | `vibeserve-perf-profiler` |
| Agent prompt | `profilers/perf.j2` |

Filesystem and Python identifiers retain underscores. MCP server identifiers normalize
underscores to dashes, so `macos_cpu` produces `vibeserve-macos-cpu-profiler`.

`auto` and `none` are modes, not runnable profiler definitions.

## Implement the support package

Create `resources/profilers/<kind>/server.py`. The MCP server should expose tools for
capability detection, diagnostic collection, and useful report analysis. Tool results must
use structured diagnostics for unavailable tools, permissions, or unsupported facilities.

Profiling must remain separate from the trusted scored benchmark. Persist raw artifacts
and reproduction metadata rather than embedding unbounded profiler output in the agent
response. Target the process that performs the workload, including child workers when
necessary.

## Add the profiler prompt

Create `src/vibe_serve/loops/agent/templates/profilers/<kind>.j2`. Explain how the
agent should collect and interpret evidence, which limitations it must report, and how it
should produce the shared `ProfilerSummary`. The agent and evolve loops resolve this prompt
by convention.

## Validate the implementation

Test domain and interface compatibility, registry-derived packaging names, MCP tool
registration, capability and failure paths, artifact metadata, and prompt selection. Add
platform-gated integration coverage when collection depends on host tooling.

A conventional profiler must not require new context fields, sandbox mount branches, CLI
flags, or MCP/prompt dispatch mappings. If it does, first determine whether the requirement
is a reusable framework capability or a profiler-local implementation detail.
