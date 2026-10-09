"""Run-scoped provider substitution: the fallback a run has switched to, and where it applies.

Sessions cannot move between providers, so a switch never changes a session that
is already open. It is recorded here and applied when a role opens its next
session, which is a fresh conversation on the fallback provider and model. That
makes the clean boundary the session boundary: the hypothesis whose turn hit the
limit ends there, and the next one starts on the fallback.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_runtime._agent_execution import AgentExecutionConfiguration


@dataclass(frozen=True, slots=True)
class FallbackTarget:
    """The provider and model a run may switch to when its own provider has no capacity."""

    provider: str
    model: str

    def __post_init__(self) -> None:
        """Reject an empty provider or model: a fallback names both."""
        if not self.provider or not self.model:
            message = "a fallback names both a provider and a model"
            raise ValueError(message)


class ProviderFallback:
    """Which providers this run has replaced by its fallback, shared by every session opener.

    Thread-safe: roles run on separate worker threads and the operator's resume
    arrives on a third.
    """

    def __init__(self, target: FallbackTarget | None) -> None:
        """Start with no provider replaced; ``target`` is None when none is configured."""
        self._target = target
        self._replaced: set[str] = set()
        self._lock = threading.Lock()

    @property
    def target(self) -> FallbackTarget | None:
        """The configured fallback, if any."""
        return self._target

    def replaced(self, provider: str) -> bool:
        """Whether sessions opened from now on use the fallback instead of ``provider``."""
        with self._lock:
            return provider in self._replaced

    def replace_provider(self, provider: str) -> bool:
        """Replace ``provider`` by the fallback; True when this call made the switch.

        Raises when no fallback is configured or when ``provider`` is the
        fallback itself, since neither could change what a session runs on.
        """
        if self._target is None:
            message = "no fallback is configured"
            raise ValueError(message)
        if provider == self._target.provider:
            message = f"{provider!r} is already the fallback provider"
            raise ValueError(message)
        with self._lock:
            if provider in self._replaced:
                return False
            self._replaced.add(provider)
            return True

    def apply(self, configuration: AgentExecutionConfiguration) -> AgentExecutionConfiguration:
        """The configuration a session opened now runs with.

        A replaced provider's spec takes the fallback provider and model. The
        per-role model and reasoning-effort overrides named the old provider's
        models, so they are dropped rather than carried across.
        """
        target = self._target
        if target is None or not self.replaced(configuration.spec.provider):
            return configuration
        spec = replace(
            configuration.spec,
            provider=target.provider,
            model=target.model,
            role_models={},
            reasoning_effort=None,
            role_reasoning_efforts={},
        )
        return replace(configuration, spec=spec, reasoning_effort=None)
