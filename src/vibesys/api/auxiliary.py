"""Public contracts for run-attached auxiliary agents.

These agents support product surfaces such as experiment chat.  They are not
the orchestration API: plugins declare :class:`vs_runtime.api.AgentRole` values
and create sessions through ``Run.agents`` instead.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from vibesys.api.store import RunRecord

AgentDriver = Literal["agentshim", "omnigent"]


class AuxiliaryAgentDriver(BaseModel):
    """One available auxiliary-agent driver and its accepted providers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    driver: AgentDriver
    providers: tuple[str, ...]

    @field_validator("providers")
    @classmethod
    def _valid_providers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            message = "auxiliary agent driver must support at least one provider"
            raise ValueError(message)
        if any(not provider.strip() for provider in value):
            message = "auxiliary agent providers must not be empty"
            raise ValueError(message)
        if len(set(value)) != len(value):
            message = "auxiliary agent providers must be unique"
            raise ValueError(message)
        return value


class RunReady(BaseModel):
    """Stable facts a frontend needs once a run has acquired its resources.

    ``record`` is the run's semantic read model; project storage stays private.
    ``log_directory`` is where a frontend attaches its event projection.
    ``frontend_state_directory`` is private durable state for that frontend.
    The remaining fields describe the configured default for optional
    run-attached agents without exposing the run's configuration or sandbox.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    record: RunRecord
    log_directory: Path
    frontend_state_directory: Path
    agent_driver: AgentDriver
    agent_provider: str
    agent_model: str
    agent_drivers: tuple[AuxiliaryAgentDriver, ...]
    role_models: tuple[str, ...] = ()

    @field_validator("log_directory", "frontend_state_directory")
    @classmethod
    def _absolute_path(cls, value: Path) -> Path:
        """Keep frontend attachment locations absolute and normalized."""
        return value.expanduser().resolve(strict=False)

    @model_validator(mode="after")
    def _valid_agent_defaults(self) -> Self:
        drivers = {item.driver: item.providers for item in self.agent_drivers}
        if len(drivers) != len(self.agent_drivers):
            message = "auxiliary agent drivers must be unique"
            raise ValueError(message)
        providers = drivers.get(self.agent_driver)
        if providers is None:
            message = f"default auxiliary agent driver is unavailable: {self.agent_driver!r}"
            raise ValueError(message)
        if self.agent_provider not in providers:
            message = (
                f"agent driver {self.agent_driver!r} does not support provider "
                f"{self.agent_provider!r}; supported providers: {', '.join(providers)}"
            )
            raise ValueError(message)
        return self


class AuxiliaryReadableInput(BaseModel):
    """One read-only host path exposed through a stable environment variable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    environment_variable: str
    purpose: str

    @field_validator("path")
    @classmethod
    def _absolute_path(cls, value: Path) -> Path:
        return value.expanduser().resolve(strict=False)

    @field_validator("environment_variable")
    @classmethod
    def _valid_environment_variable(cls, value: str) -> str:
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", value) is None:
            message = "auxiliary readable input environment_variable must be uppercase shell syntax"
            raise ValueError(message)
        return value

    @field_validator("purpose")
    @classmethod
    def _nonempty_purpose(cls, value: str) -> str:
        if not value.strip():
            message = "auxiliary readable input purpose must not be empty"
            raise ValueError(message)
        return value


class AuxiliaryAgentLaunch(BaseModel):
    """Session-fixed policy for one product-owned auxiliary conversation.

    Repeated turns on the returned ``ManagedAgent`` continue one context.
    Calling ``RunSession.create_auxiliary_agent`` again starts a fresh context.
    The optional continuation prompt is fixed at creation and is used only
    while the provider confirms that the original conversation still exists.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str
    member_id: str
    driver: AgentDriver
    provider: str
    model: str
    system_prompt: str
    continuation_prompt: str | None = None
    readable_inputs: tuple[AuxiliaryReadableInput, ...] = ()

    @field_validator("role", "member_id", "driver", "provider", "model", "system_prompt")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value.strip():
            message = "auxiliary agent launch fields must not be empty"
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def _unique_readable_inputs(self) -> Self:
        paths = tuple(item.path for item in self.readable_inputs)
        variables = tuple(item.environment_variable for item in self.readable_inputs)
        if len(set(paths)) != len(paths):
            message = "auxiliary agent readable input paths must be unique"
            raise ValueError(message)
        if len(set(variables)) != len(variables):
            message = "auxiliary agent readable input environment variables must be unique"
            raise ValueError(message)
        return self


class ManagedAgent(Protocol):
    """One explicitly owned auxiliary agent conversation.

    ``turn`` calls serialize.  Every call after the first reuses this object's
    provider context when the driver can do so.  ``close`` is idempotent.
    """

    def turn(self, message: str, *, invocation_id: str | None = None) -> str:
        """Send one follow-on message in this agent's conversation."""
        ...

    def close(self) -> None:
        """Release this agent and its environment."""
        ...


class AuxiliaryAgents(Protocol):
    """Independent caller-owned scope for run-attached conversations.

    A scope remains usable after its originating run completes. The caller
    closes it to release every conversation; close is idempotent and future
    construction fails with RuntimeError.
    """

    def create_auxiliary_agent(self, launch: AuxiliaryAgentLaunch) -> ManagedAgent:
        """Create a conversation owned by this scope."""
        ...

    def close(self) -> None:
        """Close every scope-owned conversation."""
        ...


__all__ = [
    "AgentDriver",
    "AuxiliaryAgentDriver",
    "AuxiliaryAgentLaunch",
    "AuxiliaryAgents",
    "AuxiliaryReadableInput",
    "ManagedAgent",
    "RunReady",
]
