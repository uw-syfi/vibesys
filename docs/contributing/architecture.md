# Architecture: Python module graph

[`tach.toml`](https://github.com/uw-syfi/vibesys/blob/main/tach.toml) enforces
the Python module graph, and CI runs `uv run tach check`. Modules are stacked in
four layers (`entrypoints`, `server`, `vibesys`, `libs`; top to bottom). An
import from a higher layer fails, which forbids core -> server, server ->
entrypoints, libs -> core, and anything -> entrypoints. Finer ordering inside
`server` and inside `vibesys` comes from `depends_on` (every same-layer edge and
every root file is pinned) plus `forbid_circular_dependencies`. Modules cover
`src/` (`entrypoints`, `server.*`, `vibesys.*`) and every `libs/*/src`.

The graphs below are generated from `tach.toml` by `tach show --mermaid`. CI
fails when they are stale. To refresh after editing `tach.toml`:

```bash
uv run python scripts/check_tach_graph.py --write
```

Views: a package-level overview, the `vibesys` core modules, and the full
module graph. The graph is acyclic and `tach.toml` forbids cycles.

[//]: # (tach-graph:start)
## Architecture overview

Submodules such as `vibesys.agents` and `server.api` are collapsed into their top-level package.

```mermaid
graph TD
    vs_project --> vs_loop_state
```

## Core layers

Edges among the `vibesys` core modules. The graph is acyclic; `tach.toml` forbids cycles.

```mermaid
graph TD
    vibesys.agents --> vibesys
    vibesys.agents --> vibesys.config
    vibesys.agents --> vibesys.constants
    vibesys.agents --> vibesys.events
    vibesys.agents --> vibesys.render
    vibesys.agents --> vibesys.schemas
    vibesys.agents --> vibesys.skills
    vibesys.backends --> vibesys.constants
    vibesys.backends --> vibesys.profilers
    vibesys.config --> vibesys.constants
    vibesys.config --> vibesys.features
    vibesys.config --> vibesys.repository
    vibesys.context --> vibesys.agents
    vibesys.context --> vibesys.backends
    vibesys.context --> vibesys.boot_trace
    vibesys.context --> vibesys.config
    vibesys.context --> vibesys.constants
    vibesys.context --> vibesys.domains
    vibesys.context --> vibesys.errors
    vibesys.context --> vibesys.evaluators
    vibesys.context --> vibesys.events
    vibesys.context --> vibesys.llm_client
    vibesys.context --> vibesys.profilers
    vibesys.context --> vibesys.render
    vibesys.context --> vibesys.resource_paths
    vibesys.context --> vibesys.run
    vibesys.context --> vibesys.sandbox
    vibesys.domains --> vibesys.constants
    vibesys.domains --> vibesys.prompts
    vibesys.evaluators --> vibesys.constants
    vibesys.evaluators --> vibesys.resource_paths
    vibesys.input_project --> vibesys.sdk_paths
    vibesys.llm_client --> vibesys.config
    vibesys.llm_client --> vibesys.constants
    vibesys.loops --> vibesys.agents
    vibesys.loops --> vibesys.config
    vibesys.loops --> vibesys.constants
    vibesys.loops --> vibesys.context
    vibesys.loops --> vibesys.domains
    vibesys.loops --> vibesys.evaluators
    vibesys.loops --> vibesys.events
    vibesys.loops --> vibesys.profilers
    vibesys.loops --> vibesys.prompts
    vibesys.loops --> vibesys.render
    vibesys.loops --> vibesys.run
    vibesys.loops --> vibesys.sandbox
    vibesys.loops --> vibesys.schemas
    vibesys.loops --> vibesys.skills
    vibesys.profilers --> vibesys.constants
    vibesys.profilers --> vibesys.linux_cpu_profiler
    vibesys.profilers --> vibesys.macos_cpu_profiler
    vibesys.prompts --> vibesys.constants
    vibesys.render --> vibesys.constants
    vibesys.render --> vibesys.events
    vibesys.resource_paths --> vibesys.constants
    vibesys.run --> vibesys.agents
    vibesys.run --> vibesys.backends
    vibesys.run --> vibesys.config
    vibesys.run --> vibesys.constants
    vibesys.run --> vibesys.evaluators
    vibesys.run --> vibesys.events
    vibesys.run --> vibesys.input_project
    vibesys.run --> vibesys.profilers
    vibesys.run --> vibesys.render
    vibesys.run --> vibesys.repository
    vibesys.run --> vibesys.sandbox
    vibesys.run --> vibesys.skills
    vibesys.sandbox --> vibesys.agents
    vibesys.sandbox --> vibesys.backends
    vibesys.sandbox --> vibesys.constants
    vibesys.sandbox --> vibesys.domains
    vibesys.sandbox --> vibesys.evaluators
    vibesys.sandbox --> vibesys.profilers
    vibesys.sandbox --> vibesys.prompts
    vibesys.sandbox --> vibesys.skypilot
    vibesys.sdk_paths --> vibesys.constants
    vibesys.skills --> vibesys.constants
    vibesys.skills --> vibesys.schemas
    vibesys.skypilot --> vibesys.unix_socket
```

## Full module graph

```mermaid
graph TD
    server.api --> server.chat
    server.api --> server.controller
    server.api --> server.diagnostics
    server.api --> server.events
    server.api --> server.execution
    server.api --> server.integration
    server.api --> server.journal
    server.api --> server.run_lifecycle
    server.api --> server.settings
    server.chat --> server.controller
    server.chat --> server.events
    server.chat --> server.execution
    server.chat --> server.journal
    server.chat --> server.run_lifecycle
    server.controller --> server.diagnostics
    server.controller --> server.events
    server.controller --> server.execution
    server.controller --> server.journal
    server.controller --> server.run_lifecycle
    server.events --> server.diagnostics
    server.events --> server.event_index
    server.events --> server.run_lifecycle
    server.execution --> server.diagnostics
    server.execution --> server.events
    server.execution --> server.journal
    server.integration --> server.chat
    server.integration --> server.controller
    server.integration --> server.diagnostics
    server.integration --> server.events
    server.integration --> server.execution
    server.integration --> server.journal
    server.integration --> server.read_model
    server.integration --> server.run_lifecycle
    server.journal --> server.diagnostics
    server.journal --> server.events
    server.read_model --> server.controller
    server.read_model --> server.events
    server.runtime --> server.api
    server.runtime --> server.chat
    server.runtime --> server.controller
    server.runtime --> server.diagnostics
    server.runtime --> server.events
    server.runtime --> server.execution
    server.runtime --> server.integration
    server.runtime --> server.journal
    server.runtime --> server.read_model
    server.runtime --> server.settings
    server.runtime --> server.transport
    server.tool_payloads --> server.events
    server.transport --> server.api
    vibesys.agents --> vibesys
    vibesys.agents --> vibesys.config
    vibesys.agents --> vibesys.constants
    vibesys.agents --> vibesys.events
    vibesys.agents --> vibesys.render
    vibesys.agents --> vibesys.schemas
    vibesys.agents --> vibesys.skills
    vibesys.backends --> vibesys.constants
    vibesys.backends --> vibesys.profilers
    vibesys.config --> vibesys.constants
    vibesys.config --> vibesys.features
    vibesys.config --> vibesys.repository
    vibesys.context --> vibesys.agents
    vibesys.context --> vibesys.backends
    vibesys.context --> vibesys.boot_trace
    vibesys.context --> vibesys.config
    vibesys.context --> vibesys.constants
    vibesys.context --> vibesys.domains
    vibesys.context --> vibesys.errors
    vibesys.context --> vibesys.evaluators
    vibesys.context --> vibesys.events
    vibesys.context --> vibesys.llm_client
    vibesys.context --> vibesys.profilers
    vibesys.context --> vibesys.render
    vibesys.context --> vibesys.resource_paths
    vibesys.context --> vibesys.run
    vibesys.context --> vibesys.sandbox
    vibesys.domains --> vibesys.constants
    vibesys.domains --> vibesys.prompts
    vibesys.evaluators --> vibesys.constants
    vibesys.evaluators --> vibesys.resource_paths
    vibesys.input_project --> vibesys.sdk_paths
    vibesys.llm_client --> vibesys.config
    vibesys.llm_client --> vibesys.constants
    vibesys.loops --> vibesys.agents
    vibesys.loops --> vibesys.config
    vibesys.loops --> vibesys.constants
    vibesys.loops --> vibesys.context
    vibesys.loops --> vibesys.domains
    vibesys.loops --> vibesys.evaluators
    vibesys.loops --> vibesys.events
    vibesys.loops --> vibesys.profilers
    vibesys.loops --> vibesys.prompts
    vibesys.loops --> vibesys.render
    vibesys.loops --> vibesys.run
    vibesys.loops --> vibesys.sandbox
    vibesys.loops --> vibesys.schemas
    vibesys.loops --> vibesys.skills
    vibesys.profilers --> vibesys.constants
    vibesys.profilers --> vibesys.linux_cpu_profiler
    vibesys.profilers --> vibesys.macos_cpu_profiler
    vibesys.prompts --> vibesys.constants
    vibesys.render --> vibesys.constants
    vibesys.render --> vibesys.events
    vibesys.resource_paths --> vibesys.constants
    vibesys.run --> vibesys.agents
    vibesys.run --> vibesys.backends
    vibesys.run --> vibesys.config
    vibesys.run --> vibesys.constants
    vibesys.run --> vibesys.evaluators
    vibesys.run --> vibesys.events
    vibesys.run --> vibesys.input_project
    vibesys.run --> vibesys.profilers
    vibesys.run --> vibesys.render
    vibesys.run --> vibesys.repository
    vibesys.run --> vibesys.sandbox
    vibesys.run --> vibesys.skills
    vibesys.sandbox --> vibesys.agents
    vibesys.sandbox --> vibesys.backends
    vibesys.sandbox --> vibesys.constants
    vibesys.sandbox --> vibesys.domains
    vibesys.sandbox --> vibesys.evaluators
    vibesys.sandbox --> vibesys.profilers
    vibesys.sandbox --> vibesys.prompts
    vibesys.sandbox --> vibesys.skypilot
    vibesys.sdk_paths --> vibesys.constants
    vibesys.skills --> vibesys.constants
    vibesys.skills --> vibesys.schemas
    vibesys.skypilot --> vibesys.unix_socket
    vs_project --> vs_loop_state
```
[//]: # (tach-graph:end)
