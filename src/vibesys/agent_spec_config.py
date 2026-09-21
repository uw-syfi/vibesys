"""Build an :class:`~vs_agent.spec.AgentSpec` from application ``Config``.

This is the core-side adapter between VibeSys configuration and the leaf
``vs_agent`` library: it reads ``[agent]``/``[model]``/``[thinking]`` config and
resolves one fully-formed ``AgentSpec``. It lives in core (not ``vs_agent``)
because it depends on ``vibesys.config``; the library never imports core.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_agent.api import DEFAULT_CLI_PROVIDER, AgentBackend, AgentSpec, Driver

if TYPE_CHECKING:
    from vibesys.config import Config


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
    """Resolve one :class:`AgentSpec` the way application config always has.

    Precedence at every field is override, then ``[agent]`` config, then the
    VibeSys default: CLI backend, the agentshim driver, the codex provider. The
    deprecated ``[model].provider`` stays ignored, exactly as it always has.
    """
    agent_cfg = config.agent
    resolved_backend = AgentBackend(backend or agent_cfg.backend or AgentBackend.CLI)
    resolved_driver = Driver(driver) if driver is not None else resolve_agent_driver(config)

    if resolved_backend != AgentBackend.CLI and agent_cfg.driver is not None:
        raise SystemExit(  # noqa: TRY003  # tracked: #288
            f"agent driver {agent_cfg.driver!r} is valid only with backend='cli', "
            f"not {resolved_backend.value!r}"
        )

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
