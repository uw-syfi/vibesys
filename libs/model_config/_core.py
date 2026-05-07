"""Single source of truth for model provider parsing and representation.

ModelConfig is the canonical way to represent a provider+model pair across
all SDS runtimes (pydantic-ai, litellm, LangChain).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Alias → canonical provider mapping
# ---------------------------------------------------------------------------

_ALIAS_TO_CANONICAL: dict[str, str] = {
    # OpenAI family
    "openai": "openai",
    "codex": "openai",
    "opencode": "openai",
    # Anthropic family
    "anthropic": "anthropic",
    "claude": "anthropic",
    "claude-code": "anthropic",
    # Gemini (Google AI Studio)
    "gemini": "gemini",
    "rlm": "gemini",
    # Vertex AI
    "vertex": "vertex",
}

_CANONICAL_PROVIDERS = frozenset({"openai", "anthropic", "gemini", "vertex"})

# Providers that have no canonical family and must use heuristics only
_UNRESOLVABLE_ALIASES = frozenset({"subagent", "hybrid"})

# ---------------------------------------------------------------------------
# Prefix maps for string parsing
# ---------------------------------------------------------------------------

# Ordered so longest prefixes are checked first (google-vertex before openai etc.)
_COLON_PREFIX_MAP: list[tuple[str, str]] = [
    ("google-vertex:", "vertex"),
    ("google-gla:", "gemini"),
    ("anthropic:", "anthropic"),
    ("openai:", "openai"),
]

_SLASH_PREFIX_MAP: list[tuple[str, str]] = [
    ("vertex_ai/", "vertex"),
    ("gemini/", "gemini"),
    ("anthropic/", "anthropic"),
    ("openai/", "openai"),
]


# ---------------------------------------------------------------------------
# ModelConfig dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelConfig:
    """Canonical representation of a provider + model pair.

    Attributes:
        provider: Canonical family — one of "openai", "anthropic", "gemini", "vertex".
        model: Bare model name, e.g. "gpt-4o", "claude-3-7-sonnet-latest".
        location: Vertex AI region (used only for vertex provider).
        thinking_budget: Token budget for extended thinking (positive int or None).
    """

    provider: str
    model: str
    location: str | None = None
    thinking_budget: int | None = None

    def __post_init__(self) -> None:
        if self.provider not in _CANONICAL_PROVIDERS:
            raise ValueError(
                f"provider must be one of {sorted(_CANONICAL_PROVIDERS)}, got {self.provider!r}. "
                "Use from_provider_and_model() or from_string() to map SDS aliases."
            )
        if not self.model or not self.model.strip():
            raise ValueError("model must be a non-empty string")
        if self.thinking_budget is not None and (
            not isinstance(self.thinking_budget, int) or self.thinking_budget <= 0  # pyright: ignore[reportUnnecessaryIsInstance]
        ):
            raise ValueError(f"thinking_budget must be a positive int, got {self.thinking_budget!r}")

    # ------------------------------------------------------------------
    # Factory: from SDS provider alias + bare model name
    # ------------------------------------------------------------------

    @classmethod
    def from_provider_and_model(
        cls,
        provider: str,
        model: str,
        *,
        location: str | None = None,
        thinking_budget: int | None = None,
    ) -> ModelConfig:
        """Build a ModelConfig from an SDS provider alias and bare model name.

        Raises:
            ValueError: If the alias has no canonical mapping (e.g. "subagent", "hybrid").
        """
        alias = provider.lower()
        if alias in _UNRESOLVABLE_ALIASES:
            raise ValueError(
                f"Provider {provider!r} has no canonical family mapping. "
                "Use from_string() with provider_hint for heuristic resolution."
            )
        canonical = _ALIAS_TO_CANONICAL.get(alias)
        if canonical is None:
            raise ValueError(f"Unknown provider alias {provider!r}. Known aliases: {sorted(_ALIAS_TO_CANONICAL)}")
        return cls(provider=canonical, model=model, location=location, thinking_budget=thinking_budget)

    # ------------------------------------------------------------------
    # Factory: from model string (with optional hints)
    # ------------------------------------------------------------------

    @classmethod
    def from_string(
        cls,
        model_str: str,
        *,
        provider_hint: str | None = None,
        location: str | None = None,
        thinking_budget: int | None = None,
    ) -> ModelConfig:
        """Parse a model string into a ModelConfig.

        Resolution order:
        1. Colon-prefix (pydantic-ai style): ``google-vertex:``, ``google-gla:``,
           ``anthropic:``, ``openai:``.
        2. Slash-prefix (litellm style): ``vertex_ai/``, ``gemini/``,
           ``anthropic/``, ``openai/``.
        3. provider_hint: canonicalise via alias map (skipped for subagent/hybrid).
        4. Heuristic: substring match on model name.
        5. Fallback: ``openai`` (preserves existing behaviour for unknown models).
        """
        lower = model_str.lower()

        # Step 1: colon-prefix
        for prefix, canonical in _COLON_PREFIX_MAP:
            if lower.startswith(prefix):
                bare = model_str[len(prefix) :]
                return cls(provider=canonical, model=bare, location=location, thinking_budget=thinking_budget)

        # Step 2: slash-prefix
        for prefix, canonical in _SLASH_PREFIX_MAP:
            if lower.startswith(prefix):
                bare = model_str[len(prefix) :]
                return cls(provider=canonical, model=bare, location=location, thinking_budget=thinking_budget)

        # Step 3: provider_hint
        if provider_hint is not None and provider_hint.lower() not in _UNRESOLVABLE_ALIASES:
            canonical = _ALIAS_TO_CANONICAL.get(provider_hint.lower())
            if canonical is not None:
                return cls(provider=canonical, model=model_str, location=location, thinking_budget=thinking_budget)

        # Step 4: heuristics
        if "claude" in lower:
            return cls(provider="anthropic", model=model_str, location=location, thinking_budget=thinking_budget)
        if "gpt" in lower or "o1" in lower or "o3" in lower:
            return cls(provider="openai", model=model_str, location=location, thinking_budget=thinking_budget)
        if "gemini" in lower:
            return cls(provider="gemini", model=model_str, location=location, thinking_budget=thinking_budget)

        # Step 5: fallback
        return cls(provider="openai", model=model_str, location=location, thinking_budget=thinking_budget)

    # ------------------------------------------------------------------
    # Conversion methods
    # ------------------------------------------------------------------

    def to_pydantic_ai_str(self) -> str:
        """Return a pydantic-ai model identifier string."""
        prefix_map = {
            "openai": "openai",
            "anthropic": "anthropic",
            "gemini": "google-gla",
            "vertex": "google-vertex",
        }
        prefix = prefix_map[self.provider]
        return f"{prefix}:{self.model}"

    def to_litellm_str(self) -> str:
        """Return a litellm-compatible ``provider/model`` string."""
        prefix_map = {
            "openai": "openai",
            "anthropic": "anthropic",
            "gemini": "gemini",
            "vertex": "vertex_ai",
        }
        prefix = prefix_map[self.provider]
        return f"{prefix}/{self.model}"

    def to_pydantic_ai_settings(self, budget_tokens: int | None = None) -> dict[str, Any]:
        """Return pydantic-ai model_settings dict enabling extended thinking.

        Args:
            budget_tokens: Override for the thinking token budget. Falls back to
                           ``self.thinking_budget`` if not given.

        Returns:
            Empty dict if no budget is set or provider does not support thinking.
        """
        budget = budget_tokens if budget_tokens is not None else self.thinking_budget
        if budget is None:
            return {}
        if self.provider == "anthropic":
            return {"anthropic_thinking": {"type": "enabled", "budget_tokens": budget}}
        if self.provider == "gemini":
            return {"gemini_thinking_config": {"thinking_budget": budget, "include_thoughts": True}}
        if self.provider == "vertex":
            return {"google_thinking_config": {"thinking_budget": budget, "include_thoughts": True}}
        # openai does not support thinking config
        return {}

    # ------------------------------------------------------------------
    # Environment validation
    # ------------------------------------------------------------------

    def validate_env(self) -> None:
        """Check that required environment variables are present for this provider.

        Raises:
            EnvironmentError: With a descriptive message listing any missing variables.
        """
        missing: list[str] = []

        if self.provider == "openai":
            if not os.environ.get("OPENAI_API_KEY"):
                missing.append("OPENAI_API_KEY")

        elif self.provider == "anthropic":
            if not os.environ.get("ANTHROPIC_API_KEY"):
                missing.append("ANTHROPIC_API_KEY")

        elif self.provider == "gemini":
            if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
                missing.append("GEMINI_API_KEY or GOOGLE_API_KEY")

        elif self.provider == "vertex":
            if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
                missing.append("GOOGLE_APPLICATION_CREDENTIALS")
            if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
                missing.append("GOOGLE_CLOUD_PROJECT")
            # Location: self.location → GOOGLE_CLOUD_LOCATION → VERTEX_LOCATION
            location = self.location or os.environ.get("GOOGLE_CLOUD_LOCATION") or os.environ.get("VERTEX_LOCATION")
            if not location:
                missing.append("location (set ModelConfig.location, GOOGLE_CLOUD_LOCATION, or VERTEX_LOCATION)")

        if missing:
            raise OSError(f"Missing required environment for provider {self.provider!r}: " + ", ".join(missing))


# ---------------------------------------------------------------------------
# Module-level convenience functions (re-exported from __init__)
# ---------------------------------------------------------------------------


def normalize_provider(alias: str) -> str:
    """Map an SDS provider alias to its canonical family name.

    Returns one of: "openai", "anthropic", "gemini", "vertex".
    Raises ValueError for unresolvable aliases ("subagent", "hybrid").
    """
    a = alias.lower()
    if a in _UNRESOLVABLE_ALIASES:
        raise ValueError(f"Provider {alias!r} has no canonical family mapping.")
    canonical = _ALIAS_TO_CANONICAL.get(a)
    if canonical is None:
        raise ValueError(f"Unknown provider alias {alias!r}. Known: {sorted(_ALIAS_TO_CANONICAL)}")
    return canonical


def from_provider_and_model(
    provider: str,
    model: str,
    *,
    location: str | None = None,
    thinking_budget: int | None = None,
) -> ModelConfig:
    """Convenience wrapper for ``ModelConfig.from_provider_and_model``."""
    return ModelConfig.from_provider_and_model(provider, model, location=location, thinking_budget=thinking_budget)


def from_string(
    model_str: str,
    *,
    provider_hint: str | None = None,
    location: str | None = None,
    thinking_budget: int | None = None,
) -> ModelConfig:
    """Convenience wrapper for ``ModelConfig.from_string``."""
    return ModelConfig.from_string(
        model_str,
        provider_hint=provider_hint,
        location=location,
        thinking_budget=thinking_budget,
    )
