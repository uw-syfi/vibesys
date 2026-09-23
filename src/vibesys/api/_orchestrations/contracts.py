"""Execution contract and registry for orchestration implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import ValidationError

from vibesys.api.contracts import OrchestrationDescriptor

if TYPE_CHECKING:
    from vibesys.api.contracts import RunRequest
    from vibesys.run.integration import LocalRunIntegration


class Orchestration(Protocol):
    """Execute a run without exposing its internal agent topology to the caller."""

    def execute(self, request: RunRequest, integration: LocalRunIntegration) -> bool: ...


class OrchestrationRegistry:
    """Map stable orchestration IDs to implementations."""

    def __init__(self) -> None:
        self._implementations: dict[str, Orchestration] = {}

    def register(self, kind: str, implementation: Orchestration) -> None:
        try:
            OrchestrationDescriptor(id=kind, config_version=1, options={})
        except ValidationError as exc:
            msg = f"invalid orchestration ID {kind!r}"
            raise ValueError(msg) from exc
        if kind in self._implementations:
            msg = f"orchestration {kind!r} is already registered"
            raise ValueError(msg)
        self._implementations[kind] = implementation

    def resolve(self, kind: str) -> Orchestration:
        try:
            return self._implementations[kind]
        except KeyError as exc:
            msg = f"orchestration {kind!r} is not registered"
            raise ValueError(msg) from exc
