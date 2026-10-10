# vs-agent

Provider-neutral agent execution for VibeSys.

## Responsibility

This package owns agent selection and configuration, execution sessions, typed
events and usage, session persistence interfaces, and agent tool integration.
Applications choose when agents run and how their events affect a workflow.
It is the only package that imports `agentshim`: a `SessionLauncher` opens
agentshim sessions, and `shim_translation` is the one place VibeSys and agentshim
types convert into each other.

## Concepts

- `AgentSpec` is one resolved backend, provider, and model policy. It
  rejects unsupported backend/provider pairs before client construction.
- `build_agent_client` wires that policy to an execution environment and
  returns a provider-neutral client.
- `AgentEvent` and `AgentUsage` carry execution output and accounting without
  binding consumers to a provider's event format.
- `SessionStore` implementations control whether provider sessions can resume
  across turns or runs.
- `MCPServerSpec` carries the tool servers that `vs_mcp` descriptors declare
  (`ToolSpec`, `serve_stdio`, `expose_as_tools`) into an agent's tool transport,
  without putting application workflow in the launcher.

## Using the API

Import application-facing types and functions from `vs_agent.api`. Test doubles
and scripted events live in `vs_agent.api.testing`.

```python
from vs_agent.api import SHIPPED_PROVIDERS, AgentSpec

spec = AgentSpec(provider="codex")  # rejected unless in SHIPPED_PROVIDERS
```

`build_agent_client` also needs the selected sandbox, skills, session store, and
event sink. The application composition layer supplies those run-specific
resources and owns cleanup.

## Ownership Boundary

Provider-specific command execution and event translation stay here. Prompts,
run scheduling, and interpretation of agent output stay in the application.
Provider CLI policy and installation guidance live in the agent-driver docs.
