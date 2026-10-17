"""How a person reads the vetted models: each one's family, tier and display name.

:func:`vs_agent.provider_policy.harnesses` lists the provider and model pairs
VibeSys runs (agentshim owns which models a CLI runs). This module owns how
VibeSys *presents* them: models of one provider group into families (a
generation, such as "Claude 5.5"), each family orders its models from fast and
cheap to strong (its tiers), and every model has a readable name. A frontend
renders these facts and never derives them from a model id.

A vetted model with no entry here is a bug the catalog's completeness test
catches, so a new model in agentshim fails a test, not a screen.
"""

from __future__ import annotations

from dataclasses import dataclass

from vs_agent import provider_profiles


@dataclass(frozen=True, slots=True)
class ModelDisplay:
    """How one vetted model reads: its provider, family, tier and own name."""

    provider_name: str
    """The provider's CLI as people call it ("Claude Code")."""
    model_name: str
    """The model alone ("Claude Opus 5.5")."""
    family: str
    """The family's stable key ("claude-5.5"), unique across providers."""
    family_name: str
    """The family as people call it ("Claude 5.5")."""
    tier: int
    """Zero for the family's fastest and cheapest model, rising to its strongest."""
    tier_name: str
    """The tier as people call it ("Opus")."""


@dataclass(frozen=True, slots=True)
class _Tier:
    model: str
    name: str
    """The tier as people call it ("Opus")."""
    model_name: str
    """The model alone ("Claude Opus 5.5")."""


@dataclass(frozen=True, slots=True)
class _Family:
    provider: str
    key: str
    name: str
    tiers: tuple[_Tier, ...]
    """Fastest and cheapest first."""


_FAMILIES: tuple[_Family, ...] = (
    _Family("codex", "gpt-5.5", "GPT-5.5", (_Tier("gpt-5.5", "Standard", "GPT-5.5"),)),
    _Family(
        "codex",
        "gpt-5.6",
        "GPT-5.6",
        (
            _Tier("gpt-5.6-luna", "Luna", "GPT-5.6 Luna"),
            _Tier("gpt-5.6-terra", "Terra", "GPT-5.6 Terra"),
            _Tier("gpt-5.6-sol", "Sol", "GPT-5.6 Sol"),
        ),
    ),
    _Family(
        "codex",
        "gpt-6",
        "GPT-6",
        (
            _Tier("gpt-6-luna", "Luna", "GPT-6 Luna"),
            _Tier("gpt-6-sol", "Sol", "GPT-6 Sol"),
            _Tier("gpt-6-astra", "Astra", "GPT-6 Astra"),
        ),
    ),
    _Family("codex", "gpt-6.1", "GPT-6.1", (_Tier("gpt-6.1-sol", "Sol", "GPT-6.1 Sol"),)),
    _Family(
        "claude",
        "claude-4.5",
        "Claude 4.5",
        (_Tier("claude-haiku-4-5", "Haiku", "Claude Haiku 4.5"),),
    ),
    _Family(
        "claude",
        "claude-5",
        "Claude 5",
        (
            _Tier("claude-sonnet-5", "Sonnet", "Claude Sonnet 5"),
            _Tier("claude-opus-5", "Opus", "Claude Opus 5"),
            _Tier("claude-fable-5", "Fable", "Claude Fable 5"),
        ),
    ),
    _Family(
        "claude",
        "claude-5.1",
        "Claude 5.1",
        (_Tier("claude-fable-5-1", "Fable", "Claude Fable 5.1"),),
    ),
    _Family(
        "claude",
        "claude-5.5",
        "Claude 5.5",
        (
            _Tier("claude-haiku-5-5", "Haiku", "Claude Haiku 5.5"),
            _Tier("claude-sonnet-5-5", "Sonnet", "Claude Sonnet 5.5"),
            _Tier("claude-opus-5-5", "Opus", "Claude Opus 5.5"),
        ),
    ),
    _Family(
        "opencode",
        "gemini-3",
        "Gemini 3",
        (
            _Tier(
                "google-vertex/gemini-3-pro-preview",
                "Pro (preview)",
                "Gemini 3 Pro (preview)",
            ),
        ),
    ),
)


def model_display(provider: str, model: str) -> ModelDisplay | None:
    """How ``model`` of ``provider`` reads, or ``None`` for a pair the catalog does not describe."""
    for family in _FAMILIES:
        if family.provider != provider:
            continue
        for rank, tier in enumerate(family.tiers):
            if tier.model == model:
                return ModelDisplay(
                    provider_name=provider_profiles.provider_profile(provider).display_name,
                    model_name=tier.model_name,
                    family=family.key,
                    family_name=family.name,
                    tier=rank,
                    tier_name=tier.name,
                )
    return None
