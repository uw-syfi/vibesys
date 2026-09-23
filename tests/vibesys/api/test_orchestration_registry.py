"""Selection and isolation contracts for the orchestration boundary."""

from __future__ import annotations

import pytest

from vibesys.api._dispatch import dispatch_loop
from vibesys.api._orchestrations.builtins import built_in_orchestrations
from vibesys.api._orchestrations.contracts import OrchestrationRegistry
from vibesys.api.contracts import LoopKind, RunRequest
from vibesys.run.integration import LocalRunIntegration


class _StubOrchestration:
    def __init__(self, *, result: bool) -> None:
        self.result = result
        self.calls: list[tuple[RunRequest, LocalRunIntegration]] = []

    def execute(self, request: RunRequest, integration: LocalRunIntegration) -> bool:
        self.calls.append((request, integration))
        return self.result


def test_registry_rejects_duplicate_and_missing_ids() -> None:
    registry = OrchestrationRegistry()
    implementation = _StubOrchestration(result=True)
    registry.register(LoopKind.PLAIN, implementation)

    assert registry.resolve(LoopKind.PLAIN) is implementation
    with pytest.raises(ValueError, match="already registered"):
        registry.register(LoopKind.PLAIN, _StubOrchestration(result=False))
    with pytest.raises(ValueError, match="not registered"):
        registry.resolve(LoopKind.EVOLVE)


def test_dispatch_uses_injected_registry_without_built_in_loop_calls() -> None:
    registry = OrchestrationRegistry()
    implementation = _StubOrchestration(result=False)
    registry.register(LoopKind.EVOLVE, implementation)
    request = RunRequest.model_construct(loop=LoopKind.EVOLVE)
    integration = LocalRunIntegration()

    assert dispatch_loop(request, integration, registry) is False
    assert implementation.calls == [(request, integration)]


def test_builtin_ids_resolve_to_their_own_implementations() -> None:
    registry = built_in_orchestrations()

    assert registry.resolve(LoopKind.AGENT) is registry.resolve(LoopKind.PROFILE_GUIDED)
    assert registry.resolve(LoopKind.PLAIN) is not registry.resolve(LoopKind.EVOLVE)
    assert registry.resolve(LoopKind.AGENT) is not registry.resolve(LoopKind.PLAIN)
