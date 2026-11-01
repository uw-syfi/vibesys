"""Scripted stand-ins for provider limits: ``FakeLimitsReader``, and a process fake for the real reader.

``FakeLimitsReader`` answers from readings a test gives it and keeps a credit book, so a test of
what the host does with limits runs with no CLI. It spends the way a provider does: a credit once
per idempotency key, after which every window reads empty and the credit is gone.

``limits_executor`` is the other half: agentshim's fake ``claude`` and ``codex app-server``
processes behind one executor, for running ``AgentLimitsReader`` itself with no CLI. Both pass the
same contract suite (``libs/vs-agent/tests/test_usage_limits.py``).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import PurePath
from typing import TYPE_CHECKING

from agentshim.testing import FakeExecutor

from vs_agent.api.usage_limits import (
    ProviderLimits,
    SpendOutcome,
    SpendResult,
    Unavailable,
    UnavailableReason,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agentshim.execution.process import SpawnRequest
    from agentshim.testing import ClaudeStreamPeers, CodexScript
    from agentshim.testing.process import FakePeer


class FakeLimitsReader:
    """A ``LimitsReader`` that answers from a script; unknown providers are ``UNSUPPORTED``."""

    def __init__(
        self,
        readings: Mapping[str, ProviderLimits | Sequence[ProviderLimits]],
        *,
        holding: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        """Answer ``readings`` by provider; ``holding`` are the reset credit ids each provider holds.

        A provider given a sequence answers its readings in order and repeats the last one.
        """
        self._readings = {
            provider: [reading] if isinstance(reading, ProviderLimits) else list(reading)
            for provider, reading in readings.items()
        }
        self._credits = {provider: tuple(ids) for provider, ids in (holding or {}).items()}
        self._spent: dict[str, list[str]] = {}
        self._answers: dict[tuple[str, str], SpendOutcome] = {}
        #: Each provider read, in order.
        self.reads: list[str] = []
        #: Each spend asked, as ``(provider, idempotency_key, credit_id)``.
        self.spends: list[tuple[str, str, str | None]] = []

    def read(self, provider: str) -> ProviderLimits:
        """The scripted reading, with spent credits applied."""
        self.reads.append(provider)
        script = self._readings.get(provider)
        reading = None if not script else (script.pop(0) if len(script) > 1 else script[0])
        if reading is None:
            return ProviderLimits(
                provider=provider,
                unavailable=Unavailable(UnavailableReason.UNSUPPORTED, "no reading scripted"),
            )
        if reading.unavailable is not None:
            return reading
        held = self._held(provider)
        spent = bool(self._spent.get(provider))
        return replace(
            reading,
            windows=tuple(replace(w, used_fraction=0.0) for w in reading.windows)
            if spent
            else reading.windows,
            reset_credits_available=None if reading.reset_credits_available is None else len(held),
            reset_credits=tuple(c for c in reading.reset_credits if c.id in held),
        )

    def spend(self, provider: str, *, idempotency_key: str, credit_id: str | None) -> SpendResult:
        """Spend a credit for ``idempotency_key`` once; a repeat of a key repeats its answer."""
        if not idempotency_key.strip():
            message = "a reset needs an idempotency key"
            raise ValueError(message)
        if provider not in self._credits:
            message = f"{provider} has no reset credits"
            raise ValueError(message)
        self.spends.append((provider, idempotency_key, credit_id))
        key = (provider, idempotency_key)
        if key not in self._answers:
            held = self._held(provider)
            chosen = credit_id if credit_id is not None else (held[0] if held else None)
            if chosen is None or chosen not in held:
                self._answers[key] = SpendOutcome.NO_CREDIT
            else:
                self._spent.setdefault(provider, []).append(chosen)
                self._answers[key] = SpendOutcome.RESET
        return SpendResult(outcome=self._answers[key], limits=self.read(provider))

    def _held(self, provider: str) -> list[str]:
        spent = self._spent.get(provider, [])
        return [credit for credit in self._credits.get(provider, ()) if credit not in spent]


def limits_executor(
    *, claude: ClaudeStreamPeers | None = None, codex: CodexScript | None = None
) -> FakeExecutor:
    """An executor whose ``claude`` and ``codex`` are agentshim's fake CLIs, for ``AgentLimitsReader``."""

    def peers(request: SpawnRequest) -> FakePeer:
        binary = PurePath(request.argv[0]).name
        if binary == "claude" and claude is not None:
            return claude.build(request)
        if binary == "codex" and codex is not None:
            return codex.peer(request)
        message = f"no fake {binary} was given"
        raise ValueError(message)

    return FakeExecutor([], peers=peers)
