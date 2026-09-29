"""Server-owned enumeration of the chat agent selections a run offers.

Clients render what this module produces and enumerate nothing themselves.
The agent *driver* is deliberately absent from the result: which driver backs
a run is a deployment detail, so every chat thread inherits the run's, and the
options describe only the CLI providers that driver supports plus the models
worth suggesting under each.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from vibesys.api import SUGGESTED_MODELS

if TYPE_CHECKING:
    from vibesys.api import AgentDriver, AuxiliaryAgentDriver

ChatModelSource = Literal["run", "role", "suggested"]


class _ChatOptionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatModelOption(_ChatOptionModel):
    """One offered chat model and the source of its suggestion."""

    model: str
    source: ChatModelSource
    default: bool = False


class ChatProviderOptions(_ChatOptionModel):
    """One supported chat provider and its suggested models."""

    provider: str
    models: list[ChatModelOption] = Field(default_factory=list)


class ChatOptions(_ChatOptionModel):
    """All offered chat selections grouped by provider."""

    providers: list[ChatProviderOptions] = Field(default_factory=list)


@dataclass(frozen=True)
class ChatRunSettings:
    """The run's own agent selection, from which chat options are derived.

    ``role_models`` are the selected plugin's ``[agent.roles.<id>]`` overrides:
    a run that deliberately gives one role a different model is naming a model
    its operator already trusts for this workspace.
    """

    driver: AgentDriver
    provider: str
    model: str
    agent_drivers: tuple[AuxiliaryAgentDriver, ...]
    role_models: tuple[str, ...] = field(default=())

    def providers_for(self, driver: AgentDriver) -> tuple[str, ...]:
        """Return the snapshotted providers for one available driver."""
        choice = next((item for item in self.agent_drivers if item.driver == driver), None)
        if choice is None:
            message = f"auxiliary agent driver is unavailable: {driver!r}"
            raise ValueError(message)
        return choice.providers


def build_chat_options(settings: ChatRunSettings) -> ChatOptions:
    """Enumerate the providers and model suggestions this run's chat offers.

    Only the run's configured driver is considered, so a client never has to
    know that drivers exist. The run's own model is always present and is the
    single option marked ``default``.
    """
    return ChatOptions(
        providers=[
            ChatProviderOptions(provider=provider, models=_models_for(provider, settings))
            for provider in settings.providers_for(settings.driver)
        ]
    )


def _models_for(provider: str, settings: ChatRunSettings) -> list[ChatModelOption]:
    """Order one provider's options: run model, role overrides, suggestions.

    The run model and role overrides are configured against the run's own
    provider, so they are offered only there; another provider gets its
    suggestion list alone.
    """
    options: list[ChatModelOption] = []
    seen: set[str] = set()

    def add(model: str, source: ChatModelSource, *, default: bool = False) -> None:
        name = model.strip()
        if not name or name in seen:
            return
        seen.add(name)
        options.append(ChatModelOption(model=name, source=source, default=default))

    if provider == settings.provider:
        add(settings.model, "run", default=True)
        for role_model in settings.role_models:
            add(role_model, "role")
    for suggestion in SUGGESTED_MODELS.get(provider, ()):
        add(suggestion, "suggested")
    return options
