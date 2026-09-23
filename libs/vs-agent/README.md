# vs-agent

Driver-neutral agent execution for VibeSys.

## Responsibility

This package owns agent selection and configuration, execution sessions, typed
events and usage, session persistence interfaces, and agent tool integration.
Applications choose when agents run and how their events affect a workflow.
The package adapts supported drivers behind a common client contract.

## Concepts

- `AgentSpec` is one resolved backend, driver, provider, and model policy. It
  rejects unsupported driver/provider pairs before client construction.
- `build_agent_client` wires that policy to an execution environment and
  returns a driver-neutral client.
- `AgentEvent` and `AgentUsage` carry execution output and accounting without
  binding consumers to a provider's event format.
- `SessionStore` implementations control whether provider sessions can resume
  across turns or runs.
- `ToolSpec`, `MCPServerSpec`, and the stdio helpers expose application tools to
  agents without putting application workflow in the driver.

## Using the API

Import application-facing types and functions from `vs_agent.api`. Test doubles
and scripted events live in `vs_agent.api.testing`.

```python
from vs_agent.api import AgentSpec, Driver, agent_catalog

spec = AgentSpec(driver=Driver.AGENTSHIM)
providers = agent_catalog()[spec.driver].providers
```

`build_agent_client` also needs the selected sandbox, skills, session store, and
event sink. The application composition layer supplies those run-specific
resources and owns cleanup.

## Ownership Boundary

Provider-specific command execution and event translation stay here. Prompts,
run scheduling, and interpretation of agent output stay in the application.
Provider CLI policy and installation guidance live in the agent-driver docs.
