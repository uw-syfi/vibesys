"""Execution contract and registry for orchestration implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from vibesys.api.contracts import LoopKind, RunRequest
    from vibesys.run.integration import LocalRunIntegration


class Orchestration(Protocol):
    """Execute a run without exposing its internal agent topology to the caller."""

    def execute(self, request: RunRequest, integration: LocalRunIntegration) -> bool: ...


class OrchestrationRegistry:
    """Map stable loop IDs to implementations, rejecting duplicate registrations."""

    def __init__(self) -> None:
        self._implementations: dict[LoopKind, Orchestration] = {}

    def register(self, kind: LoopKind, implementation: Orchestration) -> None:
        if kind in self._implementations:
            msg = f"orchestration {kind.value!r} is already registered"
            raise ValueError(msg)
        self._implementations[kind] = implementation

    def resolve(self, kind: LoopKind) -> Orchestration:
        try:
            return self._implementations[kind]
        except KeyError as exc:
            msg = f"orchestration {kind.value!r} is not registered"
            raise ValueError(msg) from exc
