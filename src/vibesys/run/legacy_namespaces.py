"""Compatibility names for the built-in policies' persisted namespaces."""

from enum import StrEnum


class RunStateNamespace(StrEnum):
    """Stable legacy namespace names retained for existing loop callers."""

    AGENT = "agent"
    EVOLVE = "evolve"
    PLAIN = "plain"
    RUNTIME = "runtime"
    SKYPILOT = "skypilot"
