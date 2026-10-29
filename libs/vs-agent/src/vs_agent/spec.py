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
from vs_agent.provider_policy import (
    DEFAULT_CLI_PROVIDER,
    SHIPPED_PROVIDERS,
    check_allowed_model,
    check_harness,
)
from vs_agent.session_environment import validate_env_names

if TYPE_CHECKING:
    from collections.abc import Mapping


class AgentBackend(StrEnum):
    """Agent runner backend ``build_agent_client`` can build a client for."""

    CLI = "cli"
    STUB = "stub"
    SCRIPTED = "scripted"
    """Agent-less runs: scripted replies stand in for the model (see ``ScriptedAgentClient``)."""


@dataclass(frozen=True)
class AgentSpec:
    """One resolved agent policy: backend, provider, and model.

    ``__post_init__`` rejects a provider AgentShim cannot run, and a CLI
    model outside the vetted harnesses unless ``allow_unvetted_model`` is
    set, so either fails at construction rather than partway through a run.
    A non-empty ``allowed_models`` narrows that further for a CLI spec, and
    ``allow_unvetted_model`` does not bypass it. ``dataclasses.replace`` runs
    the same checks, so a model swapped in later (a role override, a quota
    fallback) is held to them too. Docker support is not validated here:
    whether the agent can run inside a container depends on the run
    environment, which ``build_agent_client`` (not this value type) knows
    about.
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
    #: The operator's explicit opt-out of the vetted harness list, set only by
    #: the terminal's ``--allow-unvetted-model`` and by a resume, which keeps
    #: the harness its run was started with.
    allow_unvetted_model: bool = False
    #: The seed of the ``scripted`` backend's choices; no other backend reads it.
    scripted_seed: int = 0
    #: Models a CLI spec may use. Empty means the host set no restriction.
    #: ``allow_unvetted_model`` does not bypass a non-empty set.
    allowed_models: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        """Reject an unsupported provider, an unvetted model, or a model outside the host set."""
        validate_env_names(self.env_passthrough)
        supported = sorted(SHIPPED_PROVIDERS)
        if self.provider not in supported:
            message = (
                f"agent provider {self.provider!r} is not supported; "
                f"supported providers: {', '.join(supported)}"
            )
            raise ValueError(message)
        if self.backend is not AgentBackend.CLI:
            return
        if not self.allow_unvetted_model:
            for model in (self.model, *self.role_models.values()):
                if model is not None:
                    check_harness(self.provider, model)
        if self.model is not None:
            check_allowed_model(self.model, role=None, allowed=self.allowed_models)
        for role, model in self.role_models.items():
            check_allowed_model(model, role=role, allowed=self.allowed_models)
