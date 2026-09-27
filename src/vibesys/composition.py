"""Product-owned wiring between VibeSys configuration and runtime adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from vibesys.profilers import tool_server as profiler_tool_server
from vs_agent.api import (
    DEFAULT_CLI_PROVIDER,
    AgentBackend,
    AgentSpec,
    Driver,
    ToolServerDescriptor,
    expose_as_tools,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys.config import Config
    from vibesys.profilers import ProfilerKind
    from vs_runtime.api import Workspace


@dataclass(frozen=True, slots=True)
class AgentToolContext:
    """Product facts available while binding one declared agent tool."""

    profiler_kind: ProfilerKind


def resolve_agent_driver(config: Config) -> Driver:
    """Resolve the configured agent driver, defaulting to agentshim."""
    return Driver(config.agent.driver) if config.agent.driver is not None else Driver.AGENTSHIM


def agent_spec_from_config(
    config: Config,
    *,
    backend: AgentBackend | str | None = None,
    driver: Driver | str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> AgentSpec:
    """Bind application config and explicit overrides into one agent spec."""
    agent_cfg = config.agent
    resolved_backend = AgentBackend(backend or agent_cfg.backend or AgentBackend.CLI)
    resolved_driver = Driver(driver) if driver is not None else resolve_agent_driver(config)

    if resolved_backend != AgentBackend.CLI and agent_cfg.driver is not None:
        message = f"agent driver {agent_cfg.driver!r} is valid only with backend='cli', not {resolved_backend.value!r}"
        raise SystemExit(message)

    resolved_provider = provider or agent_cfg.cli_provider or DEFAULT_CLI_PROVIDER
    return AgentSpec(
        backend=resolved_backend,
        driver=resolved_driver,
        provider=resolved_provider,
        model=model if model is not None else config.model.name,
        role_models={
            role: configured
            for role, configured in {
                "orchestrator": agent_cfg.outer.model,
                "implementer": agent_cfg.inner.model,
            }.items()
            if configured is not None
        },
        reasoning_effort=config.thinking.level,
        cli_timeout=agent_cfg.cli_timeout,
        role_reasoning_efforts={
            role: configured
            for role, configured in {
                "orchestrator": agent_cfg.outer.reasoning_effort,
                "implementer": agent_cfg.inner.reasoning_effort,
            }.items()
            if configured is not None
        },
    )


def _profiler_tool(context: object, _workspace: Workspace) -> tuple[ToolServerDescriptor, ...]:
    """Bind the selected profiler's analysis server to one agent session."""
    resolved = cast("AgentToolContext", context)
    spec = profiler_tool_server(resolved.profiler_kind)
    return () if spec is None else (spec,)


def _issue_board_tool(_host: object, _workspace: Workspace) -> tuple[ToolServerDescriptor, ...]:
    """Bind the fixed issue-board server to workspace-relative policy artifacts."""
    return (
        expose_as_tools(
            name="vibesys-issue-board",
            entrypoint_module="vibesys.orchestration.issue_queue.tool_server",
            entrypoint_args=(
                "issues.json",
                ".vibesys/issue-tool-policy.json",
                ".vibesys/issue-tracker.json",
            ),
        ),
    )


AGENT_TOOL_BINDINGS: Mapping[
    str, Callable[[object, Workspace], tuple[ToolServerDescriptor, ...]]
] = {
    "issue-board": _issue_board_tool,
    "profiler": _profiler_tool,
}
"""Built-in agent tools bound by product composition, not orchestration policy."""
