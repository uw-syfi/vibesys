# Architecture: Python module graph

[`tach.toml`](https://github.com/uw-syfi/vibesys/blob/main/tach.toml) freezes
the Python module graph, and CI runs `uv run python scripts/check_tach.py`. An import between
modules that is not a declared `depends_on` edge fails the check. Modules cover
`src/` (`entrypoints`, `launch`, `headless`, `server.*`, `vibesys.*`) and every `libs/*/src`.
Run it through the wrapper, not `tach check` directly: tach treats a library with
its own `pyproject.toml` (a uv workspace member) as a separate package and stops
checking its edges and interfaces, and the wrapper runs tach on a copy of the
source roots without those files.

The graph below is generated from `tach.toml` by `tach show --mermaid`. CI
fails when it is stale. To refresh after editing `tach.toml`:

```bash
uv run python scripts/check_tach_graph.py --write
```

A package-level overview with submodules collapsed to their top-level package.
The graph is acyclic and `tach.toml` forbids cycles.

Every direct subfolder of `src/vibesys/orchestration/` is one orchestration
strategy: `dynamic`, `evolve`, `issue_queue`, `multi`, or `single`. Shared
policy and resources (`domains`, `hypothesis`, `profile_focus`,
`prompts`, and `metrics`) live beside `orchestration` under `src/vibesys/` and
never import it. Domain templates live in their domain package; strategy
templates live in their strategy package. The shared `prompts` package owns
only shared rendering and templates. A strategy may exclusively own a run shell
when every source consumer of that shell belongs to the strategy. Shared run
composition and peer strategies cannot import such a shell. Its downward policy
interfaces and library dependencies remain declared in Tach. Top-level orchestration modules remain
shared policy helpers; they are not strategy packages.

A strategy written as a flow (today `dynamic`) has this layout inside its folder:

- `search/`: the `@search` root flow, its `Role` declarations beside it, its config, prompt
  contexts and the pure helpers its flows share. `search/api.py` is the only public surface;
  other modules start with an underscore and nothing outside the package imports them. Code here
  reaches `vs_core` only through `vs_core.api.search`.
- `core_policy/`: host-side policy that reads the wider `vs_core.api` (evidence requirements,
  prompt variables, the projection of the published `RunSnapshot`), with the prompt templates.
- `launch.py` and `models.py`: the options an operator writes (the search config plus the
  task's inputs) and the reply models.

What a task fixes (gates, metric space, stage limits) is `vs_core.api.TaskTerms` on the run's
facts, not search config. The old `strategy/` state-machine package is gone for `dynamic`; the
other strategies keep it until they are ported. See [orchestration-runtime.md](orchestration-runtime.md)
and [flows.md](flows.md); `tests/architecture/test_search_packages.py` enforces the rules.

`src/vibesys/` holds strategies, domain resources, prompts, and thin wiring.
The generic pure lifecycle lives in `vs_core`, published only through
`vs_core.api`: immutable state, events, requests, strategy contracts and projections.
The `vs_runtime` async shell calls strategy decisions and core transitions
separately, persists intent before I/O, and returns typed observations.
`vs_core` has no I/O-library dependencies. Infrastructure mechanisms belong in
`vs_runtime` and its libraries. Placement
test: "Would another product built on vs_runtime need this mechanism, with no
VibeSys policy in it? Then it belongs in a library, not src/vibesys."

Design stateful systems as a pure core that emits requests and a thin async
shell that calls I/O interfaces and returns typed outcomes as events. Each
owning library declares its interface in its `.api` and holds interchangeable
implementations inside it or in smaller libraries it depends on. The shell
imports the core and owning libraries; the core imports no I/O library. Follow
the software-design rule
[Functional core, interfaces and implementations](https://github.com/uw-syfi/vibesys/blob/main/.agents/skills/software-design/references/functional-core.md)
for durable intent, recovery, and tests.

Execution environments, command runners, and agent confinement have separate
owners; see [Sandboxing and confinement](sandboxing.md).

Long-running work writes its output to `vs_logs`, which keeps each stream under a typed
owner for humans to read. `vs_logs` sits in the tach layer `logs`, above every library, so
no library imports it; only the product writes logs and only `server.host_helper` reads
them (`vs_logs.read`). See [Log streams](logs.md).

The TypeScript clients under `clients/` have their own package graph, layers, and
build model; see [Clients architecture](clients-architecture.md).

`vibesys.orchestration` owns built-in orchestration policy. Explicit plugins,
including the issue queue, live under the singular
`vibesys.orchestration.<plugin>` namespace; `launch` assembles their built-in
catalog from individual registrations exposed by `vibesys.api.catalog`.
`vibesys.plugin_catalog` retains the explicit registry contract.
Agent roles, plan and reply schemas, prompts, and pure strategy decisions
live with their owning orchestration; generic lifecycle and recovery decisions
belong in the pure core. For example, the
hypothesis planner's skill-selection and title rules are public through
`vibesys.hypothesis`, not a top-level schema catch-all. Generic
run roles live in `vibesys.api`: `Runs` starts and resumes independent
`RunHandle` tasks, which publish semantic events, accept stop requests, and
return results. `launch.default_runs` selects VibeSys implementations;
`vs-runtime` owns the generic task and event-stream mechanism. Only
entrypoints, tests and scripts import `launch`. Headless receives one started
handle; the server receives `Runs`. The transitional `vibesys.api.session`
contract retains queries, readiness and auxiliary-agent access;
workspace, persistence, and sandbox mechanisms live in the runtime libraries.
The `vibesys.api` package root is policy-neutral. Applications opt into a
built-in policy through a named facade such as `vibesys.api.hypothesis` or
`vibesys.api.evolve`; the generic facade does not re-export those contracts.
Tach records each dependency. `vs_project` owns generic project layout and
persistence operations. Hypothesis-search history and its serialization belong
to `vibesys.hypothesis`; they are policy contracts, not generic lifecycle types.
The v5 manifest separates policy-specific descriptor options from the generic
`execution` record. The latter is derived from `RunRequest` and resolved host
settings, including the concrete profiler. Resume checks it before setup.
Trusted evaluator execution and package mechanics live in the runtime and
sandbox libraries. Orchestration owns gate cadence and result interpretation.

Libraries are converted to uv workspace members bottom-up (a library converts
once every library it imports is a member). A member's `pyproject.toml` declares
exactly the third-party packages its `src/` imports, and reaches other members
through `[tool.uv.sources] <name> = { workspace = true }`.
`scripts/check_member_dependencies.py` fails CI on an undeclared or unused
dependency, an undeclared member edge, or an import of a root-distribution
package. The `vibesys` wheel still bundles every library's source, so each
member dependency must also appear in the root `dependencies` with the same
version constraint.

The strategy and plugin contract is in
[orchestration-runtime.md](orchestration-runtime.md).

A search is `@search(name, config=Model)` over a flow (straight-line `async` code that the
runtime journals and replays). It follows [flows.md](flows.md): the `ctx` vocabulary (agents,
workspaces, evaluation, groups), the rule for flow code, how core ends the run from the winner
the flow chose, and how to test it.

[//]: # (tach-graph:start)
## Architecture overview

Submodules such as `vibesys.orchestration` and `server.api` are collapsed into their top-level package.

```mermaid
graph TD
    entrypoints --> headless
    entrypoints --> launch
    entrypoints --> server
    entrypoints --> vibesys
    entrypoints --> vs_agent
    entrypoints --> vs_decode
    entrypoints --> vs_github
    entrypoints --> vs_issue_tracker
    entrypoints --> vs_logs
    entrypoints --> vs_mcp
    entrypoints --> vs_project
    entrypoints --> vs_proto
    entrypoints --> vs_runlog
    entrypoints --> vs_runtime
    entrypoints --> vs_sandbox
    entrypoints --> vs_sim
    entrypoints --> vs_slurm
    headless --> vibesys
    launch --> vibesys
    launch --> vs_agent
    launch --> vs_evaluation
    launch --> vs_github
    launch --> vs_mcp
    launch --> vs_project
    launch --> vs_runtime
    launch --> vs_sandbox
    launch --> vs_sim
    launch --> vs_slurm
    server --> vibesys
    server --> vs_async_ops
    server --> vs_decode
    server --> vs_logs
    server --> vs_project
    server --> vs_prompts
    server --> vs_proto
    server --> vs_redaction
    server --> vs_sim
    server --> vs_slurm
    vibesys --> vs_agent
    vibesys --> vs_core
    vibesys --> vs_decode
    vibesys --> vs_evaluation
    vibesys --> vs_evaluator_protocol
    vibesys --> vs_github
    vibesys --> vs_issue_tracker
    vibesys --> vs_logs
    vibesys --> vs_mcp
    vibesys --> vs_project
    vibesys --> vs_prompts
    vibesys --> vs_proto
    vibesys --> vs_redaction
    vibesys --> vs_runlog
    vibesys --> vs_runtime
    vibesys --> vs_sandbox
    vibesys --> vs_sim
    vibesys --> vs_slurm
    vs_agent --> vs_decode
    vs_agent --> vs_faults
    vs_agent --> vs_mcp
    vs_agent --> vs_project
    vs_agent --> vs_prompts
    vs_agent --> vs_sandbox
    vs_agent --> vs_sim
    vs_async_ops --> vs_sim
    vs_core --> vs_proto
    vs_core --> vs_runlog
    vs_dst --> vs_core
    vs_dst --> vs_project
    vs_dst --> vs_runtime
    vs_dst --> vs_sim
    vs_evaluation --> vs_decode
    vs_evaluation --> vs_evaluator_protocol
    vs_evaluation --> vs_mcp
    vs_evaluation --> vs_project
    vs_evaluation --> vs_sim
    vs_evaluator_protocol --> vs_decode
    vs_faults --> vs_decode
    vs_faults --> vs_sim
    vs_github --> vs_decode
    vs_github --> vs_sim
    vs_issue_tracker --> vs_core
    vs_issue_tracker --> vs_decode
    vs_issue_tracker --> vs_github
    vs_issue_tracker --> vs_sim
    vs_logs --> vs_decode
    vs_logs --> vs_sim
    vs_project --> vs_decode
    vs_project --> vs_proto
    vs_project --> vs_sim
    vs_runlog --> vs_proto
    vs_runlog --> vs_sim
    vs_runtime --> vs_agent
    vs_runtime --> vs_async_ops
    vs_runtime --> vs_core
    vs_runtime --> vs_decode
    vs_runtime --> vs_evaluation
    vs_runtime --> vs_evaluator_protocol
    vs_runtime --> vs_github
    vs_runtime --> vs_issue_tracker
    vs_runtime --> vs_mcp
    vs_runtime --> vs_project
    vs_runtime --> vs_prompts
    vs_runtime --> vs_proto
    vs_runtime --> vs_redaction
    vs_runtime --> vs_runlog
    vs_runtime --> vs_sandbox
    vs_runtime --> vs_sim
    vs_runtime --> vs_slurm
    vs_sandbox --> vs_decode
    vs_sandbox --> vs_evaluation
    vs_sandbox --> vs_project
    vs_sandbox --> vs_redaction
    vs_sandbox --> vs_sim
    vs_sandbox --> vs_slurm
    vs_slurm --> vs_decode
    vs_slurm --> vs_sim
```
[//]: # (tach-graph:end)
