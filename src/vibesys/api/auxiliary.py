"""Public contracts for run-attached auxiliary agents.

These agents support product surfaces such as experiment chat.  They are not
the orchestration API: plugins declare :class:`vs_runtime.api.AgentRole` values
and create sessions through ``RunHost.agents`` instead.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class RunReady(BaseModel):
    """Stable facts a frontend needs once a run has acquired its resources.

    ``project_root`` lets a frontend open the project's public state API.
    ``log_directory`` is where a frontend attaches its own durable projection.
    The remaining fields describe the configured default for optional
    run-attached agents without exposing the run's configuration or sandbox.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    project_root: Path
    log_directory: Path
    agent_driver: str
    agent_provider: str
    agent_model: str
    role_models: tuple[str, ...] = ()

    @field_validator("project_root", "log_directory")
    @classmethod
    def _absolute_path(cls, value: Path) -> Path:
        """Keep frontend attachment locations absolute and normalized."""
        return value.expanduser().resolve(strict=False)


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
    driver: str
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


__all__ = ["AuxiliaryAgentLaunch", "AuxiliaryReadableInput", "ManagedAgent", "RunReady"]
