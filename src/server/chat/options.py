"""Server-owned enumeration of the chat agent selections a run offers.

Clients render what this module produces and enumerate nothing themselves.
The options describe the CLI providers the run supports plus the vetted models
(``vibesys.api.vetted_models``) under each; a thread runs only a vetted model,
or the run's own when its operator opted out of the vetted list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from vibesys.api import vetted_models

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

    provider: str
    model: str
    agent_providers: tuple[str, ...]
    role_models: tuple[str, ...] = field(default=())


def build_chat_options(settings: ChatRunSettings) -> ChatOptions:
    """Enumerate the providers and model suggestions this run's chat offers.

    The run's own model is always present and is the
    single option marked ``default``.
    """
    return ChatOptions(
        providers=[
            ChatProviderOptions(provider=provider, models=_models_for(provider, settings))
            for provider in settings.agent_providers
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
    for suggestion in vetted_models(provider):
        add(suggestion, "suggested")
    return options
