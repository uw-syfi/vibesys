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

from vs_agent.contracts import AgentExecutionPolicy
from vs_agent.provider_policy import DEFAULT_CLI_PROVIDER

if TYPE_CHECKING:
    from collections.abc import Mapping


class AgentBackend(StrEnum):
    """Agent runner backend ``build_agent_client`` can build a client for."""

    CLI = "cli"
    STUB = "stub"


class Driver(StrEnum):
    """External driver that runs a CLI agent provider."""

    AGENTSHIM = "agentshim"
    OMNIGENT = "omnigent"


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
        from vs_agent.catalog import agent_catalog  # avoid import cycle

        supported = agent_catalog()[self.driver].providers
        if self.provider not in supported:
            message = (
                f"agent driver {self.driver.value!r} does not support provider "
                f"{self.provider!r}; supported providers: {', '.join(supported)}"
            )
            raise ValueError(message)
