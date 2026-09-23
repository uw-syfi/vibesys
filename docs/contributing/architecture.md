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

`vibesys.api._orchestrations` contains transitional request adapters. Each
adapter calls only its corresponding implementation under `vibesys.loops`, and
the `builtins` module alone registers all adapters. Tach declares the agent,
plain, and evolve implementations as sibling modules with no imports between
them. New orchestration-specific logic, including agent configuration and
resume policy, belongs in `vibesys`, not `vs_project`. `vs_project` owns generic
project layout and persistence operations.

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
    vibesys.api --> vibesys.api._orchestrations._common
    vibesys.api --> vibesys.api._orchestrations.builtins
    vibesys.api --> vibesys.api._orchestrations.contracts
    vibesys.api --> vibesys.api._orchestrations.runner
    vibesys.api --> vibesys.api.contracts
    vibesys.api --> vibesys.domains
    vibesys.api --> vibesys.evaluators
    vibesys.api --> vibesys.loops
    vibesys.api --> vibesys.loops.agent
    vibesys.api --> vibesys.loops.evolve
    vibesys.api --> vibesys.render
    vibesys.api --> vibesys.run
    vibesys.api --> vibesys.sandbox
    vibesys.api._orchestrations._common --> vibesys
    vibesys.api._orchestrations._common --> vibesys.api.contracts
    vibesys.api._orchestrations.agent --> vibesys.api._orchestrations._common
    vibesys.api._orchestrations.agent --> vibesys.api.contracts
    vibesys.api._orchestrations.agent --> vibesys.loops.agent
    vibesys.api._orchestrations.agent --> vibesys.run
    vibesys.api._orchestrations.builtins --> vibesys.api._orchestrations.agent
    vibesys.api._orchestrations.builtins --> vibesys.api._orchestrations.contracts
    vibesys.api._orchestrations.builtins --> vibesys.api._orchestrations.evolve
    vibesys.api._orchestrations.builtins --> vibesys.api._orchestrations.plain
    vibesys.api._orchestrations.builtins --> vibesys.api.contracts
    vibesys.api._orchestrations.contracts --> vibesys.api.contracts
    vibesys.api._orchestrations.contracts --> vibesys.run
    vibesys.api._orchestrations.evolve --> vibesys.api._orchestrations._common
    vibesys.api._orchestrations.evolve --> vibesys.api.contracts
    vibesys.api._orchestrations.evolve --> vibesys.loops.evolve
    vibesys.api._orchestrations.evolve --> vibesys.run
    vibesys.api._orchestrations.plain --> vibesys.api._orchestrations._common
    vibesys.api._orchestrations.plain --> vibesys.api.contracts
    vibesys.api._orchestrations.plain --> vibesys.loops.plain
    vibesys.api._orchestrations.plain --> vibesys.run
    vibesys.api._orchestrations.runner --> vibesys.api._orchestrations.contracts
    vibesys.api._orchestrations.runner --> vibesys.api.contracts
    vibesys.api._orchestrations.runner --> vibesys.run
    vibesys.api.contracts --> vibesys
    vibesys.api.contracts --> vibesys.evaluators
    vibesys.api.contracts --> vibesys.loops
    vibesys.api.contracts --> vibesys.loops.evolve
    vibesys.api.contracts --> vibesys.sandbox
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
    vibesys.loops --> vibesys.evaluators
    vibesys.loops --> vibesys.render
    vibesys.loops --> vibesys.run
    vibesys.loops.agent --> vibesys
    vibesys.loops.agent --> vibesys.context
    vibesys.loops.agent --> vibesys.domains
    vibesys.loops.agent --> vibesys.evaluators
    vibesys.loops.agent --> vibesys.loops
    vibesys.loops.agent --> vibesys.prompts
    vibesys.loops.agent --> vibesys.render
    vibesys.loops.agent --> vibesys.run
    vibesys.loops.agent --> vibesys.sandbox
    vibesys.loops.evolve --> vibesys
    vibesys.loops.evolve --> vibesys.context
    vibesys.loops.evolve --> vibesys.domains
    vibesys.loops.evolve --> vibesys.evaluators
    vibesys.loops.evolve --> vibesys.loops
    vibesys.loops.evolve --> vibesys.prompts
    vibesys.loops.evolve --> vibesys.render
    vibesys.loops.evolve --> vibesys.run
    vibesys.loops.evolve --> vibesys.sandbox
    vibesys.loops.plain --> vibesys
    vibesys.loops.plain --> vibesys.context
    vibesys.loops.plain --> vibesys.domains
    vibesys.loops.plain --> vibesys.evaluators
    vibesys.loops.plain --> vibesys.prompts
    vibesys.loops.plain --> vibesys.render
    vibesys.loops.plain --> vibesys.run
    vibesys.loops.plain --> vibesys.sandbox
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
    vibesys.api --> vibesys.api._orchestrations._common
    vibesys.api --> vibesys.api._orchestrations.builtins
    vibesys.api --> vibesys.api._orchestrations.contracts
    vibesys.api --> vibesys.api._orchestrations.runner
    vibesys.api --> vibesys.api.contracts
    vibesys.api --> vibesys.domains
    vibesys.api --> vibesys.evaluators
    vibesys.api --> vibesys.loops
    vibesys.api --> vibesys.loops.agent
    vibesys.api --> vibesys.loops.evolve
    vibesys.api --> vibesys.render
    vibesys.api --> vibesys.run
    vibesys.api --> vibesys.sandbox
    vibesys.api --> vs_agent
    vibesys.api --> vs_loop_state
    vibesys.api --> vs_project
    vibesys.api --> vs_sandbox
    vibesys.api._orchestrations._common --> vibesys
    vibesys.api._orchestrations._common --> vibesys.api.contracts
    vibesys.api._orchestrations.agent --> vibesys.api._orchestrations._common
    vibesys.api._orchestrations.agent --> vibesys.api.contracts
    vibesys.api._orchestrations.agent --> vibesys.loops.agent
    vibesys.api._orchestrations.agent --> vibesys.run
    vibesys.api._orchestrations.builtins --> vibesys.api._orchestrations.agent
    vibesys.api._orchestrations.builtins --> vibesys.api._orchestrations.contracts
    vibesys.api._orchestrations.builtins --> vibesys.api._orchestrations.evolve
    vibesys.api._orchestrations.builtins --> vibesys.api._orchestrations.plain
    vibesys.api._orchestrations.builtins --> vibesys.api.contracts
    vibesys.api._orchestrations.contracts --> vibesys.api.contracts
    vibesys.api._orchestrations.contracts --> vibesys.run
    vibesys.api._orchestrations.evolve --> vibesys.api._orchestrations._common
    vibesys.api._orchestrations.evolve --> vibesys.api.contracts
    vibesys.api._orchestrations.evolve --> vibesys.loops.evolve
    vibesys.api._orchestrations.evolve --> vibesys.run
    vibesys.api._orchestrations.plain --> vibesys.api._orchestrations._common
    vibesys.api._orchestrations.plain --> vibesys.api.contracts
    vibesys.api._orchestrations.plain --> vibesys.loops.plain
    vibesys.api._orchestrations.plain --> vibesys.run
    vibesys.api._orchestrations.runner --> vibesys.api._orchestrations.contracts
    vibesys.api._orchestrations.runner --> vibesys.api.contracts
    vibesys.api._orchestrations.runner --> vibesys.run
    vibesys.api.contracts --> vibesys
    vibesys.api.contracts --> vibesys.evaluators
    vibesys.api.contracts --> vibesys.loops
    vibesys.api.contracts --> vibesys.loops.evolve
    vibesys.api.contracts --> vibesys.sandbox
    vibesys.api.contracts --> vs_agent
    vibesys.api.contracts --> vs_sandbox
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
    vibesys.loops --> vibesys.evaluators
    vibesys.loops --> vibesys.render
    vibesys.loops --> vibesys.run
    vibesys.loops --> vs_agent
    vibesys.loops --> vs_evaluator_protocol
    vibesys.loops --> vs_loop_state
    vibesys.loops --> vs_sandbox
    vibesys.loops.agent --> vibesys
    vibesys.loops.agent --> vibesys.context
    vibesys.loops.agent --> vibesys.domains
    vibesys.loops.agent --> vibesys.evaluators
    vibesys.loops.agent --> vibesys.loops
    vibesys.loops.agent --> vibesys.prompts
    vibesys.loops.agent --> vibesys.render
    vibesys.loops.agent --> vibesys.run
    vibesys.loops.agent --> vibesys.sandbox
    vibesys.loops.agent --> vs_agent
    vibesys.loops.agent --> vs_loop_state
    vibesys.loops.agent --> vs_project
    vibesys.loops.evolve --> vibesys
    vibesys.loops.evolve --> vibesys.context
    vibesys.loops.evolve --> vibesys.domains
    vibesys.loops.evolve --> vibesys.evaluators
    vibesys.loops.evolve --> vibesys.loops
    vibesys.loops.evolve --> vibesys.prompts
    vibesys.loops.evolve --> vibesys.render
    vibesys.loops.evolve --> vibesys.run
    vibesys.loops.evolve --> vibesys.sandbox
    vibesys.loops.evolve --> vs_agent
    vibesys.loops.evolve --> vs_loop_state
    vibesys.loops.evolve --> vs_project
    vibesys.loops.plain --> vibesys
    vibesys.loops.plain --> vibesys.context
    vibesys.loops.plain --> vibesys.domains
    vibesys.loops.plain --> vibesys.evaluators
    vibesys.loops.plain --> vibesys.prompts
    vibesys.loops.plain --> vibesys.render
    vibesys.loops.plain --> vibesys.run
    vibesys.loops.plain --> vibesys.sandbox
    vibesys.loops.plain --> vs_agent
    vibesys.loops.plain --> vs_issue_board
    vibesys.loops.plain --> vs_loop_state
    vibesys.loops.plain --> vs_project
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
