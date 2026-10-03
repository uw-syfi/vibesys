"""Resolved agent implementation used by an auxiliary run surface."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentSelection:
    """Resolved agent implementation used by an auxiliary run surface."""

    driver: str
    provider: str
    model: str
    role_models: tuple[str, ...] = ()
