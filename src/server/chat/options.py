"""Server-owned enumeration of the chat agent selections a run offers.

Clients render what this module produces and enumerate nothing themselves.
The agent *driver* is deliberately absent from the result: which driver backs
a run is a deployment detail, so every chat thread inherits the run's, and the
options describe only the CLI providers that driver supports plus the models
worth suggesting under each.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from vibesys.agents.factory import supported_cli_providers

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


# A deployment default, not the library's: this is the opencode model VibeSys
# offers first, and it is stated here so the chat surface does not depend on the
# agent CLI package for it.
_OPENCODE_DEFAULT_MODEL = "google-vertex/gemini-3-pro-preview"


# A short suggestion list, not a registry: the model a thread actually runs is
# whatever the provider's CLI accepts, and the client's free-text entry stays
# the escape hatch for anything not named here.
#
# The codex and claude slugs mirror the release-curated alias catalogs those
# CLIs ship (``omnigent.model_fallbacks``). They are duplicated rather than
# imported because ``omnigent`` is an optional extra and this query has to
# answer without it installed. Gemini ships no curated list here, so its group
# offers the free-text entry alone rather than guessed slugs.
_SUGGESTED_MODELS: dict[str, tuple[str, ...]] = {
    "codex": ("gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.5"),
    "claude": (
        "claude-fable-5",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    ),
    "gemini": (),
    "opencode": (_OPENCODE_DEFAULT_MODEL,),
}


@dataclass(frozen=True)
class ChatRunSettings:
    """The run's own agent selection, from which chat options are derived.

    ``role_models`` are the ``[agent.outer]`` / ``[agent.inner]`` overrides: a
    run that deliberately gives one loop role a different model is naming a
    model its operator already trusts for this workspace.
    """

    driver: str
    provider: str
    model: str
    role_models: tuple[str, ...] = field(default=())


def build_chat_options(settings: ChatRunSettings) -> ChatOptions:
    """Enumerate the providers and model suggestions this run's chat offers.

    Only the run's configured driver is considered, so a client never has to
    know that drivers exist. The run's own model is always present and is the
    single option marked ``default``.
    """
    return ChatOptions(
        providers=[
            ChatProviderOptions(provider=provider, models=_models_for(provider, settings))
            for provider in supported_cli_providers(settings.driver)
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
    for suggestion in _SUGGESTED_MODELS.get(provider, ()):
        add(suggestion, "suggested")
    return options
