# Architecture: Python module graph

[`tach.toml`](https://github.com/uw-syfi/vibesys/blob/main/tach.toml) freezes
the Python module graph, and CI runs `uv run tach check`. An import between
modules that is not a declared `depends_on` edge fails the check. Modules cover
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
    entrypoints --> headless
    entrypoints --> server
    entrypoints --> vibesys
    entrypoints --> vs_agent
    entrypoints --> vs_github
    entrypoints --> vs_project
    headless --> vibesys
    server --> vibesys
    server --> vs_agent
    server --> vs_loop_state
    server --> vs_project
    server --> vs_sandbox
    vibesys --> vs_agent
    vibesys --> vs_evaluator_protocol
    vibesys --> vs_feature_flags
    vibesys --> vs_github
    vibesys --> vs_issue_board
    vibesys --> vs_loop_state
    vibesys --> vs_project
    vibesys --> vs_prompts
    vibesys --> vs_sandbox
    vs_agent --> vs_loop_state
    vs_agent --> vs_project
    vs_agent --> vs_sandbox
    vs_project --> vs_loop_state
```

## Core layers

Edges among the `vibesys` core modules. The graph is acyclic; `tach.toml` forbids cycles.

```mermaid
graph TD
    vibesys.api --> vibesys
    vibesys.api --> vibesys.domains
    vibesys.api --> vibesys.evaluators
    vibesys.api --> vibesys.loops
    vibesys.api --> vibesys.render
    vibesys.api --> vibesys.run
    vibesys.api --> vibesys.sandbox
    vibesys.backends --> vibesys
    vibesys.context --> vibesys
    vibesys.context --> vibesys.backends
    vibesys.context --> vibesys.domains
    vibesys.context --> vibesys.evaluators
    vibesys.context --> vibesys.render
    vibesys.context --> vibesys.run
    vibesys.context --> vibesys.sandbox
    vibesys.domains --> vibesys
    vibesys.domains --> vibesys.prompts
    vibesys.evaluators --> vibesys
    vibesys.loops --> vibesys
    vibesys.loops --> vibesys.context
    vibesys.loops --> vibesys.domains
    vibesys.loops --> vibesys.evaluators
    vibesys.loops --> vibesys.prompts
    vibesys.loops --> vibesys.render
    vibesys.loops --> vibesys.run
    vibesys.loops --> vibesys.sandbox
    vibesys.prompts --> vibesys
    vibesys.render --> vibesys
    vibesys.run --> vibesys
    vibesys.run --> vibesys.backends
    vibesys.run --> vibesys.evaluators
    vibesys.run --> vibesys.render
    vibesys.run --> vibesys.sandbox
    vibesys.sandbox --> vibesys
    vibesys.sandbox --> vibesys.backends
    vibesys.sandbox --> vibesys.domains
    vibesys.sandbox --> vibesys.evaluators
    vibesys.sandbox --> vibesys.prompts
    vibesys.sandbox --> vibesys.skypilot
    vibesys.skypilot --> vibesys
```

## Full module graph

```mermaid
graph TD
    entrypoints --> headless
    entrypoints --> server.runtime
    entrypoints --> server.settings
    entrypoints --> vibesys.api
    entrypoints --> vs_agent
    entrypoints --> vs_github
    entrypoints --> vs_project
    headless --> vibesys.api
    server --> vs_agent
    server --> vs_project
    server --> vs_sandbox
    server.api --> server.chat
    server.api --> server.controller
    server.api --> server.diagnostics
    server.api --> server.events
    server.api --> server.execution
    server.api --> server.integration
    server.api --> server.journal
    server.api --> server.run_lifecycle
    server.api --> server.settings
    server.api --> vibesys.api
    server.api --> vs_loop_state
    server.api --> vs_project
    server.chat --> server
    server.chat --> server.controller
    server.chat --> server.events
    server.chat --> server.execution
    server.chat --> server.journal
    server.chat --> server.run_lifecycle
    server.chat --> vibesys.api
    server.chat --> vs_agent
    server.chat --> vs_project
    server.chat --> vs_sandbox
    server.controller --> server.diagnostics
    server.controller --> server.events
    server.controller --> server.execution
    server.controller --> server.journal
    server.controller --> server.run_lifecycle
    server.controller --> vs_project
    server.events --> server.diagnostics
    server.events --> server.event_index
    server.events --> server.run_lifecycle
    server.events --> vs_agent
    server.execution --> server.diagnostics
    server.execution --> server.events
    server.execution --> server.journal
    server.execution --> vibesys.api
    server.integration --> server
    server.integration --> server.chat
    server.integration --> server.controller
    server.integration --> server.diagnostics
    server.integration --> server.events
    server.integration --> server.execution
    server.integration --> server.journal
    server.integration --> server.read_model
    server.integration --> server.run_lifecycle
    server.integration --> vibesys.api
    server.integration --> vs_agent
    server.integration --> vs_project
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
    server.runtime --> vibesys.api
    server.settings --> vibesys.api
    server.tool_payloads --> server.events
    server.tool_payloads --> vs_agent
    server.transport --> server.api
    server.transport --> vs_project
    vibesys --> vs_agent
    vibesys --> vs_feature_flags
    vibesys --> vs_loop_state
    vibesys.api --> vibesys
    vibesys.api --> vibesys.domains
    vibesys.api --> vibesys.evaluators
    vibesys.api --> vibesys.loops
    vibesys.api --> vibesys.render
    vibesys.api --> vibesys.run
    vibesys.api --> vibesys.sandbox
    vibesys.api --> vs_agent
    vibesys.api --> vs_loop_state
    vibesys.api --> vs_project
    vibesys.api --> vs_sandbox
    vibesys.backends --> vibesys
    vibesys.backends --> vs_sandbox
    vibesys.context --> vibesys
    vibesys.context --> vibesys.backends
    vibesys.context --> vibesys.domains
    vibesys.context --> vibesys.evaluators
    vibesys.context --> vibesys.render
    vibesys.context --> vibesys.run
    vibesys.context --> vibesys.sandbox
    vibesys.context --> vs_agent
    vibesys.context --> vs_project
    vibesys.context --> vs_sandbox
    vibesys.domains --> vibesys
    vibesys.domains --> vibesys.prompts
    vibesys.evaluators --> vibesys
    vibesys.evaluators --> vs_project
    vibesys.evaluators --> vs_sandbox
    vibesys.loops --> vibesys
    vibesys.loops --> vibesys.context
    vibesys.loops --> vibesys.domains
    vibesys.loops --> vibesys.evaluators
    vibesys.loops --> vibesys.prompts
    vibesys.loops --> vibesys.render
    vibesys.loops --> vibesys.run
    vibesys.loops --> vibesys.sandbox
    vibesys.loops --> vs_agent
    vibesys.loops --> vs_evaluator_protocol
    vibesys.loops --> vs_issue_board
    vibesys.loops --> vs_loop_state
    vibesys.loops --> vs_project
    vibesys.loops --> vs_sandbox
    vibesys.prompts --> vibesys
    vibesys.prompts --> vs_prompts
    vibesys.render --> vibesys
    vibesys.run --> vibesys
    vibesys.run --> vibesys.backends
    vibesys.run --> vibesys.evaluators
    vibesys.run --> vibesys.render
    vibesys.run --> vibesys.sandbox
    vibesys.run --> vs_agent
    vibesys.run --> vs_github
    vibesys.run --> vs_loop_state
    vibesys.run --> vs_project
    vibesys.run --> vs_sandbox
    vibesys.sandbox --> vibesys
    vibesys.sandbox --> vibesys.backends
    vibesys.sandbox --> vibesys.domains
    vibesys.sandbox --> vibesys.evaluators
    vibesys.sandbox --> vibesys.prompts
    vibesys.sandbox --> vibesys.skypilot
    vibesys.sandbox --> vs_agent
    vibesys.sandbox --> vs_project
    vibesys.sandbox --> vs_sandbox
    vibesys.skypilot --> vibesys
    vibesys.skypilot --> vs_project
    vs_agent --> vs_loop_state
    vs_agent --> vs_project
    vs_agent --> vs_sandbox
    vs_project --> vs_loop_state
```
[//]: # (tach-graph:end)
