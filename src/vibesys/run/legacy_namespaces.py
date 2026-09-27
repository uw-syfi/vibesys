"""Stable names for product-owned persisted runtime namespaces."""

from enum import StrEnum


class RunStateNamespace(StrEnum):
    """Namespaces shared by product runtime composition."""

    RUNTIME = "runtime"
    SKYPILOT = "skypilot"
