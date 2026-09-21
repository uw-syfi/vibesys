"""The agent policy value type: one resolved, self-validating agent spec.

Replaces the loose driver/provider/model/backend arguments
``build_agent_client`` used to resolve independently (an override, then a
``[agent]`` config field, then a hardcoded default, repeated once per field)
with a single value: by the time an :class:`AgentSpec` exists, every field is
already resolved, and a driver/provider pair that cannot run together has
already been rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from vibesys.agents.contracts import AgentExecutionPolicy
from vibesys.agents.provider_policy import DEFAULT_CLI_PROVIDER

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vibesys.config import Config


class AgentBackend(StrEnum):
    """Agent runner backend ``build_agent_client`` can build a client for."""

    CLI = "cli"
    STUB = "stub"


class Driver(StrEnum):
    """External driver that runs a CLI agent provider.

    ``MOCK`` is test infrastructure: it satisfies the same driver contract
    while streaming a deterministic playbook, so integration tests exercise
    the real client, sink, and application integration path without an agent
    CLI.
    """

    AGENTSHIM = "agentshim"
    OMNIGENT = "omnigent"
    MOCK = "mock"


def resolve_agent_driver(config: Config) -> Driver:
    """Resolve the configured agent driver, defaulting to agentshim."""
    return Driver(config.agent.driver) if config.agent.driver is not None else Driver.AGENTSHIM


@dataclass(frozen=True)
class AgentSpec:
    """One resolved agent policy: backend, driver, provider, and model.

    ``__post_init__`` rejects a provider this driver cannot run, so an
    incompatible pair fails at construction rather than partway through
    building a client. Docker support is not validated here: whether a
    driver can run inside a container depends on the run environment, which
    ``build_agent_client`` (not this value type) knows about.
    """

    backend: AgentBackend = AgentBackend.CLI
    driver: Driver = Driver.AGENTSHIM
    provider: str = DEFAULT_CLI_PROVIDER
    model: str | None = None
    role_models: Mapping[str, str] = field(default_factory=dict)
    reasoning_effort: str | None = None
    cli_timeout: int | None = None
    role_reasoning_efforts: Mapping[str, str] = field(default_factory=dict)
    execution: AgentExecutionPolicy = field(default_factory=AgentExecutionPolicy)

    def __post_init__(self) -> None:
        """Reject a provider the resolved driver does not support."""
        from vibesys.agents.catalog import agent_catalog  # noqa: PLC0415  # avoid import cycle

        supported = agent_catalog()[self.driver].providers
        if self.provider not in supported:
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"agent driver {self.driver.value!r} does not support provider "
                f"{self.provider!r}; supported providers: {', '.join(supported)}"
            )

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        backend: AgentBackend | str | None = None,
        driver: Driver | str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> AgentSpec:
        """Resolve one :class:`AgentSpec` the way application config always has.

        Precedence at every field is override, then ``[agent]`` config, then
        the VibeSys default: CLI backend, the agentshim driver, the codex
        provider. The deprecated ``[model].provider`` stays ignored, exactly
        as it always has.

        ``mock`` drives no CLI, so its provider is always ``"mock"``
        regardless of a configured or overridden ``cli_provider``: the mock
        driver only labels a run with whatever provider was requested, it
        never runs one, so :meth:`build_agent_client` forcing the label to
        match what the driver actually is keeps the client's reported
        provider truthful.
        """
        agent_cfg = config.agent
        resolved_backend = AgentBackend(backend or agent_cfg.backend or AgentBackend.CLI)
        resolved_driver = Driver(driver) if driver is not None else resolve_agent_driver(config)

        if resolved_backend != AgentBackend.CLI and agent_cfg.driver is not None:
            raise SystemExit(  # noqa: TRY003  # tracked: #288
                f"agent driver {agent_cfg.driver!r} is valid only with backend='cli', "
                f"not {resolved_backend.value!r}"
            )

        resolved_provider = (
            "mock"
            if resolved_driver is Driver.MOCK
            else (provider or agent_cfg.cli_provider or DEFAULT_CLI_PROVIDER)
        )
        return cls(
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
