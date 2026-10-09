"""The agent policy value type: one resolved, self-validating agent spec.

Replaces the loose provider/model/backend arguments
``build_agent_client`` used to resolve independently (an override, then a
``[agent]`` config field, then a hardcoded default, repeated once per field)
with a single value: by the time an :class:`AgentSpec` exists, every field is
already resolved, and a provider AgentShim cannot run has already been
rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from vs_agent.contracts import AgentExecutionPolicy
from vs_agent.provider_policy import DEFAULT_CLI_PROVIDER, SHIPPED_PROVIDERS
from vs_agent.session_environment import validate_env_names

if TYPE_CHECKING:
    from collections.abc import Mapping


class AgentBackend(StrEnum):
    """Agent runner backend ``build_agent_client`` can build a client for."""

    CLI = "cli"
    STUB = "stub"


@dataclass(frozen=True)
class AgentSpec:
    """One resolved agent policy: backend, provider, and model.

    ``__post_init__`` rejects a provider AgentShim cannot run, so an
    unsupported provider fails at construction rather than partway through
    building a client. Docker support is not validated here: whether the
    agent can run inside a container depends on the run environment, which
    ``build_agent_client`` (not this value type) knows about.
    """

    backend: AgentBackend = AgentBackend.CLI
    provider: str = DEFAULT_CLI_PROVIDER
    model: str | None = None
    role_models: Mapping[str, str] = field(default_factory=dict)
    reasoning_effort: str | None = None
    cli_timeout: int | None = None
    role_reasoning_efforts: Mapping[str, str] = field(default_factory=dict)
    execution: AgentExecutionPolicy = field(default_factory=AgentExecutionPolicy)
    #: Launcher environment variables a session inherits beyond VibeSys's
    #: allowlist (see ``vs_agent.session_environment``).
    env_passthrough: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject a provider AgentShim does not support, or a bad env name."""
        validate_env_names(self.env_passthrough)
        supported = sorted(SHIPPED_PROVIDERS)
        if self.provider not in supported:
            message = (
                f"agent provider {self.provider!r} is not supported; "
                f"supported providers: {', '.join(supported)}"
            )
            raise ValueError(message)
