"""Single seam for reading agentshim's provider facts.

agentshim owns what a provider *is*: its binary, state directories, auth
environment variables, and container install recipe. VibeSys owns policy on
top of that: which providers it offers, which files inside a state directory
carry credentials, and what else a container needs. Every VibeSys module that
needs a provider fact reads it through here, at call time, so a library
upgrade changes behaviour without a VibeSys edit and tests have one place to
substitute a profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

    from agentshim import ProviderProfile


def _agentshim() -> ModuleType:
    """Import ``agentshim`` on first use (see :func:`provider_profile` for why)."""
    import agentshim  # noqa: PLC0415  # lint-waiver: LW-010181 [PLC0415]; Keep agentshim lazy in provider_profile so unused providers and import cycles stay unloaded.

    return agentshim


def provider_profile(provider: str) -> ProviderProfile:
    """Return the agentshim profile for *provider*.

    Imports ``agentshim`` lazily: this module is reachable from
    :mod:`vs_agent.api`'s eager exports (through :mod:`vs_agent.provider_policy`,
    :mod:`vs_agent.spec`, and others), and importing :mod:`vs_agent.api` must
    not pull in ``agentshim``.

    Raises:
        ValueError: if agentshim does not register *provider*. The message
            names the provider and the registered alternatives.
    """
    return _agentshim().get_provider(provider).profile


def provider_binary(provider: str) -> str:
    """The program name of *provider*'s CLI, as agentshim's profile names it."""
    return provider_profile(provider).binary


def launch_shell_environment() -> Mapping[str, str]:
    """The environment a run finds provider CLIs in: the interactive login shell's.

    Provider CLIs are usually installed by a shell rc file that a non-interactive
    process (an SSH command, a service) never sources, so the run reads ``PATH`` and
    credentials from the shell a user would get. agentshim reads it once per process.
    Whoever asks whether a provider CLI is installed asks here, so the answer is the
    run's.
    """
    return _agentshim().interactive_env()


def model_context_window(provider: str, model: str | None) -> int | None:
    """*model*'s context window in tokens when *provider* runs it, or ``None`` when unknown.

    agentshim owns the fact (``agentshim.context_window``). An unknown model is
    ``None``, never a guess from a similar name, so a caller shows no limit
    rather than a wrong one.
    """
    return _agentshim().context_window(provider, model)


@dataclass(frozen=True, slots=True)
class ModelDisplay:
    """How one model reads, as agentshim names it (``agentshim.ModelSpec``)."""

    model_name: str
    """The model alone ("Opus 5.5")."""
    family: str
    """The model line ("Claude", "GPT", "Gemini")."""
    tier: int | None
    """The model's rank in its family, 1 the smallest and cheapest; ``None`` off the ladder."""
    tier_name: str | None
    """The tier as people call it ("Opus"); ``None`` exactly when ``tier`` is."""


def provider_display_name(provider: str) -> str:
    """*provider*'s CLI as people call it ("Claude Code"), from agentshim's profile."""
    return provider_profile(provider).display_name


def model_display(provider: str, model: str | None) -> ModelDisplay | None:
    """How *model* run through *provider* reads, or ``None`` when agentshim does not name it.

    agentshim owns the names (``agentshim.model_spec``). An unnamed model is ``None``,
    never a prettified id: a caller shows the raw id.
    """
    spec = _agentshim().model_spec(provider, model)
    if spec is None or spec.display_name is None or spec.family is None:
        return None
    return ModelDisplay(
        model_name=spec.display_name,
        family=spec.family,
        tier=spec.tier,
        tier_name=spec.tier_name,
    )
