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

`vibesys.orchestration` owns built-in orchestration policy and the thin
product-side composition needed to run and project plugins. Explicit plugins,
including the issue queue, live under the singular
`vibesys.orchestration.<plugin>` namespace; `vibesys.plugin_catalog` registers
them. Agent roles, plan and reply schemas, prompts, state transitions, and
resume policy live with their owning orchestration. For example, the
hypothesis planner's skill-selection and title rules are public through
`vibesys.orchestration.hypothesis`, not a top-level schema catch-all. Generic
session, workspace, persistence, and sandbox mechanisms live in the runtime
libraries. Tach records each dependency. `vs_project` owns generic project
layout and persistence operations.
The v5 manifest separates policy-specific descriptor options from the generic
`execution` record. The latter is derived from `RunRequest` and resolved host
settings, including the concrete profiler. Resume checks it before setup.
Trusted gates and metric contracts live in `vibesys.evaluators`; policies call
them through the run host or import their typed results directly.

The internal custom-policy execution contract and example are in
[orchestration-runtime.md](orchestration-runtime.md).

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
    entrypoints --> vs_issue_tracker
    entrypoints --> vs_project
    headless --> vibesys
    server --> vibesys
    server --> vs_agent
    server --> vs_loop_state
    server --> vs_project
    vibesys --> vs_agent
    vibesys --> vs_github
    vibesys --> vs_issue_tracker
    vibesys --> vs_loop_state
    vibesys --> vs_project
    vibesys --> vs_prompts
    vibesys --> vs_runtime
    vibesys --> vs_sandbox
    vs_agent --> vs_project
    vs_agent --> vs_sandbox
    vs_issue_tracker --> vs_github
    vs_runtime --> vs_agent
    vs_runtime --> vs_evaluator_protocol
    vs_runtime --> vs_project
    vs_runtime --> vs_sandbox
    vs_sandbox --> vs_project
```

## Core layers

Edges among the `vibesys` core modules. The graph is acyclic; `tach.toml` forbids cycles.

```mermaid
graph TD
    vibesys.api --> vibesys
    vibesys.api --> vibesys.api.contracts
    vibesys.api --> vibesys.domains
    vibesys.api --> vibesys.evaluators
    vibesys.api --> vibesys.orchestration._common
    vibesys.api --> vibesys.orchestration.contracts
    vibesys.api --> vibesys.orchestration.evolve
    vibesys.api --> vibesys.orchestration.hypothesis.readmodel
    vibesys.api --> vibesys.orchestration.request
    vibesys.api --> vibesys.orchestration.runner
    vibesys.api --> vibesys.plugin_catalog
    vibesys.api --> vibesys.run
    vibesys.api --> vibesys.sandbox
    vibesys.api.agent --> vibesys.orchestration.agent_options
    vibesys.api.agent --> vibesys.orchestration.hypothesis.readmodel
    vibesys.api.agent --> vibesys.orchestration.memory
    vibesys.api.agent --> vibesys.plugin_catalog
    vibesys.api.contracts --> vibesys
    vibesys.api.contracts --> vibesys.errors
    vibesys.api.contracts --> vibesys.orchestration.metrics
    vibesys.api.contracts --> vibesys.orchestration.request
    vibesys.api.contracts --> vibesys.orchestration.view
    vibesys.context --> vibesys
    vibesys.context --> vibesys.domains
    vibesys.context --> vibesys.errors
    vibesys.context --> vibesys.evaluators
    vibesys.context --> vibesys.orchestration.request
    vibesys.context --> vibesys.run
    vibesys.context --> vibesys.sandbox
    vibesys.domains --> vibesys
    vibesys.domains --> vibesys.prompts
    vibesys.evaluators --> vibesys
    vibesys.orchestration._common --> vibesys.orchestration.request
    vibesys.orchestration.agent_options --> vibesys.evaluators
    vibesys.orchestration.agent_options --> vibesys.orchestration.metrics
    vibesys.orchestration.agents --> vibesys
    vibesys.orchestration.agents --> vibesys.context
    vibesys.orchestration.agents --> vibesys.orchestration.request
    vibesys.orchestration.agents --> vibesys.orchestration.steering
    vibesys.orchestration.agents --> vibesys.orchestration.workspace_resources
    vibesys.orchestration.agents --> vibesys.run
    vibesys.orchestration.artifacts --> vibesys.orchestration.memory
    vibesys.orchestration.commands --> vibesys.orchestration.workspace_resources
    vibesys.orchestration.contracts --> vibesys.orchestration.view
    vibesys.orchestration.control --> vibesys.run
    vibesys.orchestration.evolve --> vibesys
    vibesys.orchestration.evolve --> vibesys.domains
    vibesys.orchestration.evolve --> vibesys.errors
    vibesys.orchestration.evolve --> vibesys.orchestration.evolve.population
    vibesys.orchestration.evolve --> vibesys.orchestration.metrics
    vibesys.orchestration.evolve --> vibesys.orchestration.profilers
    vibesys.orchestration.evolve --> vibesys.orchestration.review
    vibesys.orchestration.evolve --> vibesys.prompts
    vibesys.orchestration.evolve.population --> vibesys.orchestration.gates
    vibesys.orchestration.evolve.population --> vibesys.orchestration.metrics
    vibesys.orchestration.evolve.population --> vibesys.orchestration.profilers
    vibesys.orchestration.gates --> vibesys
    vibesys.orchestration.gates --> vibesys.context
    vibesys.orchestration.gates --> vibesys.orchestration.request
    vibesys.orchestration.gates --> vibesys.orchestration.workspace_resources
    vibesys.orchestration.hypothesis --> vibesys.orchestration.gates
    vibesys.orchestration.hypothesis --> vibesys.orchestration.metrics
    vibesys.orchestration.hypothesis --> vibesys.orchestration.profile_focus
    vibesys.orchestration.hypothesis.readmodel --> vibesys.orchestration.hypothesis
    vibesys.orchestration.hypothesis.readmodel --> vibesys.orchestration.view
    vibesys.orchestration.issue_queue --> vibesys.orchestration.resume
    vibesys.orchestration.multi --> vibesys
    vibesys.orchestration.multi --> vibesys.domains
    vibesys.orchestration.multi --> vibesys.errors
    vibesys.orchestration.multi --> vibesys.evaluators
    vibesys.orchestration.multi --> vibesys.orchestration.agent_options
    vibesys.orchestration.multi --> vibesys.orchestration.gates
    vibesys.orchestration.multi --> vibesys.orchestration.hypothesis
    vibesys.orchestration.multi --> vibesys.orchestration.hypothesis.readmodel
    vibesys.orchestration.multi --> vibesys.orchestration.memory
    vibesys.orchestration.multi --> vibesys.orchestration.profile_focus
    vibesys.orchestration.multi --> vibesys.orchestration.profilers
    vibesys.orchestration.multi --> vibesys.orchestration.resume
    vibesys.orchestration.multi --> vibesys.orchestration.review
    vibesys.orchestration.multi --> vibesys.prompts
    vibesys.orchestration.request --> vibesys
    vibesys.orchestration.request --> vibesys.evaluators
    vibesys.orchestration.request --> vibesys.sandbox
    vibesys.orchestration.resume --> vibesys.errors
    vibesys.orchestration.runner --> vibesys.orchestration.contracts
    vibesys.orchestration.runner --> vibesys.orchestration.request
    vibesys.orchestration.runner --> vibesys.run
    vibesys.orchestration.runner --> vibesys.run.host
    vibesys.orchestration.single --> vibesys
    vibesys.orchestration.single --> vibesys.domains
    vibesys.orchestration.single --> vibesys.errors
    vibesys.orchestration.single --> vibesys.evaluators
    vibesys.orchestration.single --> vibesys.orchestration.agent_options
    vibesys.orchestration.single --> vibesys.orchestration.gates
    vibesys.orchestration.single --> vibesys.orchestration.hypothesis
    vibesys.orchestration.single --> vibesys.orchestration.hypothesis.readmodel
    vibesys.orchestration.single --> vibesys.orchestration.memory
    vibesys.orchestration.single --> vibesys.orchestration.profile_focus
    vibesys.orchestration.single --> vibesys.orchestration.profilers
    vibesys.orchestration.single --> vibesys.orchestration.resume
    vibesys.orchestration.single --> vibesys.orchestration.review
    vibesys.orchestration.single --> vibesys.prompts
    vibesys.orchestration.state --> vibesys
    vibesys.orchestration.state --> vibesys.orchestration.view
    vibesys.orchestration.workspace_resources --> vibesys
    vibesys.orchestration.workspace_resources --> vibesys.context
    vibesys.orchestration.workspace_resources --> vibesys.orchestration.request
    vibesys.plugin_catalog --> vibesys.orchestration.contracts
    vibesys.plugin_catalog --> vibesys.orchestration.evolve
    vibesys.plugin_catalog --> vibesys.orchestration.issue_queue
    vibesys.plugin_catalog --> vibesys.orchestration.multi
    vibesys.plugin_catalog --> vibesys.orchestration.single
    vibesys.prompts --> vibesys
    vibesys.prompts --> vibesys.evaluators
    vibesys.run --> vibesys
    vibesys.run --> vibesys.evaluators
    vibesys.run --> vibesys.sandbox
    vibesys.run.host --> vibesys
    vibesys.run.host --> vibesys.context
    vibesys.run.host --> vibesys.orchestration.agents
    vibesys.run.host --> vibesys.orchestration.commands
    vibesys.run.host --> vibesys.orchestration.contracts
    vibesys.run.host --> vibesys.orchestration.control
    vibesys.run.host --> vibesys.orchestration.gates
    vibesys.run.host --> vibesys.orchestration.request
    vibesys.run.host --> vibesys.orchestration.skills
    vibesys.run.host --> vibesys.orchestration.state
    vibesys.run.host --> vibesys.orchestration.workspace_resources
    vibesys.run.host --> vibesys.run
    vibesys.sandbox --> vibesys
    vibesys.sandbox --> vibesys.domains
    vibesys.sandbox --> vibesys.evaluators
    vibesys.sandbox --> vibesys.prompts
```

## Full module graph

```mermaid
graph TD
    entrypoints --> headless
    entrypoints --> server.settings
    entrypoints --> vibesys.api
    entrypoints --> vs_agent
    entrypoints --> vs_github
    entrypoints --> vs_issue_tracker
    entrypoints --> vs_project
    headless --> vibesys.api
    server --> vs_agent
    server --> vs_project
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
    server.api --> vibesys.api.agent
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
    server.controller --> server.diagnostics
    server.controller --> server.events
    server.controller --> server.execution
    server.controller --> server.journal
    server.controller --> server.run_lifecycle
    server.controller --> vibesys.api
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
    server.tool_payloads --> vs_agent
    server.transport --> server.api
    server.transport --> vs_project
    vibesys --> vs_agent
    vibesys --> vs_runtime
    vibesys --> vs_sandbox
    vibesys.api --> vibesys
    vibesys.api --> vibesys.api.contracts
    vibesys.api --> vibesys.domains
    vibesys.api --> vibesys.evaluators
    vibesys.api --> vibesys.orchestration._common
    vibesys.api --> vibesys.orchestration.contracts
    vibesys.api --> vibesys.orchestration.evolve
    vibesys.api --> vibesys.orchestration.hypothesis.readmodel
    vibesys.api --> vibesys.orchestration.request
    vibesys.api --> vibesys.orchestration.runner
    vibesys.api --> vibesys.plugin_catalog
    vibesys.api --> vibesys.run
    vibesys.api --> vibesys.sandbox
    vibesys.api --> vs_agent
    vibesys.api --> vs_project
    vibesys.api --> vs_runtime
    vibesys.api --> vs_sandbox
    vibesys.api.agent --> vibesys.orchestration.agent_options
    vibesys.api.agent --> vibesys.orchestration.hypothesis.readmodel
    vibesys.api.agent --> vibesys.orchestration.memory
    vibesys.api.agent --> vibesys.plugin_catalog
    vibesys.api.agent --> vs_project
    vibesys.api.contracts --> vibesys
    vibesys.api.contracts --> vibesys.errors
    vibesys.api.contracts --> vibesys.orchestration.metrics
    vibesys.api.contracts --> vibesys.orchestration.request
    vibesys.api.contracts --> vibesys.orchestration.view
    vibesys.api.contracts --> vs_agent
    vibesys.api.contracts --> vs_loop_state
    vibesys.api.contracts --> vs_project
    vibesys.context --> vibesys
    vibesys.context --> vibesys.domains
    vibesys.context --> vibesys.errors
    vibesys.context --> vibesys.evaluators
    vibesys.context --> vibesys.orchestration.request
    vibesys.context --> vibesys.run
    vibesys.context --> vibesys.sandbox
    vibesys.context --> vs_agent
    vibesys.context --> vs_project
    vibesys.context --> vs_runtime
    vibesys.context --> vs_sandbox
    vibesys.domains --> vibesys
    vibesys.domains --> vibesys.prompts
    vibesys.domains --> vs_sandbox
    vibesys.evaluators --> vibesys
    vibesys.evaluators --> vs_project
    vibesys.evaluators --> vs_runtime
    vibesys.evaluators --> vs_sandbox
    vibesys.orchestration._common --> vibesys.orchestration.request
    vibesys.orchestration.agent_options --> vibesys.evaluators
    vibesys.orchestration.agent_options --> vibesys.orchestration.metrics
    vibesys.orchestration.agents --> vibesys
    vibesys.orchestration.agents --> vibesys.context
    vibesys.orchestration.agents --> vibesys.orchestration.request
    vibesys.orchestration.agents --> vibesys.orchestration.steering
    vibesys.orchestration.agents --> vibesys.orchestration.workspace_resources
    vibesys.orchestration.agents --> vibesys.run
    vibesys.orchestration.agents --> vs_agent
    vibesys.orchestration.agents --> vs_runtime
    vibesys.orchestration.artifacts --> vibesys.orchestration.memory
    vibesys.orchestration.artifacts --> vs_runtime
    vibesys.orchestration.commands --> vibesys.orchestration.workspace_resources
    vibesys.orchestration.commands --> vs_runtime
    vibesys.orchestration.commands --> vs_sandbox
    vibesys.orchestration.contracts --> vibesys.orchestration.view
    vibesys.orchestration.contracts --> vs_project
    vibesys.orchestration.contracts --> vs_runtime
    vibesys.orchestration.control --> vibesys.run
    vibesys.orchestration.evolve --> vibesys
    vibesys.orchestration.evolve --> vibesys.domains
    vibesys.orchestration.evolve --> vibesys.errors
    vibesys.orchestration.evolve --> vibesys.orchestration.evolve.population
    vibesys.orchestration.evolve --> vibesys.orchestration.metrics
    vibesys.orchestration.evolve --> vibesys.orchestration.profilers
    vibesys.orchestration.evolve --> vibesys.orchestration.review
    vibesys.orchestration.evolve --> vibesys.prompts
    vibesys.orchestration.evolve --> vs_prompts
    vibesys.orchestration.evolve --> vs_runtime
    vibesys.orchestration.evolve.population --> vibesys.orchestration.gates
    vibesys.orchestration.evolve.population --> vibesys.orchestration.metrics
    vibesys.orchestration.evolve.population --> vibesys.orchestration.profilers
    vibesys.orchestration.gates --> vibesys
    vibesys.orchestration.gates --> vibesys.context
    vibesys.orchestration.gates --> vibesys.orchestration.request
    vibesys.orchestration.gates --> vibesys.orchestration.workspace_resources
    vibesys.orchestration.gates --> vs_runtime
    vibesys.orchestration.hypothesis --> vibesys.orchestration.gates
    vibesys.orchestration.hypothesis --> vibesys.orchestration.metrics
    vibesys.orchestration.hypothesis --> vibesys.orchestration.profile_focus
    vibesys.orchestration.hypothesis --> vs_loop_state
    vibesys.orchestration.hypothesis.readmodel --> vibesys.orchestration.hypothesis
    vibesys.orchestration.hypothesis.readmodel --> vibesys.orchestration.view
    vibesys.orchestration.hypothesis.readmodel --> vs_loop_state
    vibesys.orchestration.hypothesis.readmodel --> vs_runtime
    vibesys.orchestration.issue_queue --> vibesys.orchestration.resume
    vibesys.orchestration.issue_queue --> vs_issue_tracker
    vibesys.orchestration.issue_queue --> vs_runtime
    vibesys.orchestration.metrics --> vs_loop_state
    vibesys.orchestration.multi --> vibesys
    vibesys.orchestration.multi --> vibesys.domains
    vibesys.orchestration.multi --> vibesys.errors
    vibesys.orchestration.multi --> vibesys.evaluators
    vibesys.orchestration.multi --> vibesys.orchestration.agent_options
    vibesys.orchestration.multi --> vibesys.orchestration.gates
    vibesys.orchestration.multi --> vibesys.orchestration.hypothesis
    vibesys.orchestration.multi --> vibesys.orchestration.hypothesis.readmodel
    vibesys.orchestration.multi --> vibesys.orchestration.memory
    vibesys.orchestration.multi --> vibesys.orchestration.profile_focus
    vibesys.orchestration.multi --> vibesys.orchestration.profilers
    vibesys.orchestration.multi --> vibesys.orchestration.resume
    vibesys.orchestration.multi --> vibesys.orchestration.review
    vibesys.orchestration.multi --> vibesys.prompts
    vibesys.orchestration.multi --> vs_loop_state
    vibesys.orchestration.multi --> vs_prompts
    vibesys.orchestration.multi --> vs_runtime
    vibesys.orchestration.request --> vibesys
    vibesys.orchestration.request --> vibesys.evaluators
    vibesys.orchestration.request --> vibesys.sandbox
    vibesys.orchestration.request --> vs_project
    vibesys.orchestration.resume --> vibesys.errors
    vibesys.orchestration.resume --> vs_project
    vibesys.orchestration.resume --> vs_runtime
    vibesys.orchestration.runner --> vibesys.orchestration.contracts
    vibesys.orchestration.runner --> vibesys.orchestration.request
    vibesys.orchestration.runner --> vibesys.run
    vibesys.orchestration.runner --> vibesys.run.host
    vibesys.orchestration.runner --> vs_agent
    vibesys.orchestration.runner --> vs_runtime
    vibesys.orchestration.runner --> vs_sandbox
    vibesys.orchestration.single --> vibesys
    vibesys.orchestration.single --> vibesys.domains
    vibesys.orchestration.single --> vibesys.errors
    vibesys.orchestration.single --> vibesys.evaluators
    vibesys.orchestration.single --> vibesys.orchestration.agent_options
    vibesys.orchestration.single --> vibesys.orchestration.gates
    vibesys.orchestration.single --> vibesys.orchestration.hypothesis
    vibesys.orchestration.single --> vibesys.orchestration.hypothesis.readmodel
    vibesys.orchestration.single --> vibesys.orchestration.memory
    vibesys.orchestration.single --> vibesys.orchestration.profile_focus
    vibesys.orchestration.single --> vibesys.orchestration.profilers
    vibesys.orchestration.single --> vibesys.orchestration.resume
    vibesys.orchestration.single --> vibesys.orchestration.review
    vibesys.orchestration.single --> vibesys.prompts
    vibesys.orchestration.single --> vs_loop_state
    vibesys.orchestration.single --> vs_prompts
    vibesys.orchestration.single --> vs_runtime
    vibesys.orchestration.skills --> vs_runtime
    vibesys.orchestration.state --> vibesys
    vibesys.orchestration.state --> vibesys.orchestration.view
    vibesys.orchestration.workspace_resources --> vibesys
    vibesys.orchestration.workspace_resources --> vibesys.context
    vibesys.orchestration.workspace_resources --> vibesys.orchestration.request
    vibesys.orchestration.workspace_resources --> vs_runtime
    vibesys.plugin_catalog --> vibesys.orchestration.contracts
    vibesys.plugin_catalog --> vibesys.orchestration.evolve
    vibesys.plugin_catalog --> vibesys.orchestration.issue_queue
    vibesys.plugin_catalog --> vibesys.orchestration.multi
    vibesys.plugin_catalog --> vibesys.orchestration.single
    vibesys.prompts --> vibesys
    vibesys.prompts --> vibesys.evaluators
    vibesys.prompts --> vs_prompts
    vibesys.run --> vibesys
    vibesys.run --> vibesys.evaluators
    vibesys.run --> vibesys.sandbox
    vibesys.run --> vs_agent
    vibesys.run --> vs_github
    vibesys.run --> vs_project
    vibesys.run --> vs_runtime
    vibesys.run --> vs_sandbox
    vibesys.run.host --> vibesys
    vibesys.run.host --> vibesys.context
    vibesys.run.host --> vibesys.orchestration.agents
    vibesys.run.host --> vibesys.orchestration.commands
    vibesys.run.host --> vibesys.orchestration.contracts
    vibesys.run.host --> vibesys.orchestration.control
    vibesys.run.host --> vibesys.orchestration.gates
    vibesys.run.host --> vibesys.orchestration.request
    vibesys.run.host --> vibesys.orchestration.skills
    vibesys.run.host --> vibesys.orchestration.state
    vibesys.run.host --> vibesys.orchestration.workspace_resources
    vibesys.run.host --> vibesys.run
    vibesys.run.host --> vs_agent
    vibesys.run.host --> vs_runtime
    vibesys.run.host --> vs_sandbox
    vibesys.sandbox --> vibesys
    vibesys.sandbox --> vibesys.domains
    vibesys.sandbox --> vibesys.evaluators
    vibesys.sandbox --> vibesys.prompts
    vibesys.sandbox --> vs_agent
    vibesys.sandbox --> vs_project
    vibesys.sandbox --> vs_runtime
    vibesys.sandbox --> vs_sandbox
    vs_agent --> vs_project
    vs_agent --> vs_sandbox
    vs_issue_tracker --> vs_github
    vs_runtime --> vs_agent
    vs_runtime --> vs_evaluator_protocol
    vs_runtime --> vs_project
    vs_runtime --> vs_sandbox
    vs_sandbox --> vs_project
```
[//]: # (tach-graph:end)
