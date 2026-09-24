"""Agent-only API names stay available without loading them for generic imports."""

from __future__ import annotations

import subprocess
import sys
from importlib import import_module

import pytest

import vibesys.api as generic_api
from vibesys.api import agent as agent_api
from vibesys.orchestration import contracts as orchestration_contracts


def test_generic_api_import_does_not_load_builtin_policies() -> None:
    script = (
        "import sys, vibesys.api; "
        "assert 'vibesys.loops.legacy_request' not in sys.modules; "
        "assert 'vibesys.loops.registry' not in sys.modules; "
        "assert not any(name.startswith(('vibesys.loops.agent', "
        "'vibesys.loops.plain', 'vibesys.loops.evolve')) for name in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", script], check=True)  # noqa: S603


@pytest.mark.parametrize(
    "name",
    [
        "AgentRunProjection",
        "HypothesisRoundView",
        "HypothesisView",
        "agent_projection",
        "agent_run_objectives",
        "framework_memory_paths",
        "is_agent_run_manifest",
    ],
)
def test_deprecated_generic_agent_access_resolves_to_agent_facade(name: str) -> None:
    with pytest.warns(DeprecationWarning, match="vibesys.api.agent"):
        old = getattr(generic_api, name)
    assert old is getattr(agent_api, name)


def test_generic_api_preserves_legacy_star_import_names() -> None:
    assert "AgentRunProjection" in generic_api.__all__
    assert "agent_projection" in generic_api.__all__
    with pytest.raises(AttributeError):
        getattr(generic_api, "unknown_policy_name")  # noqa: B009


def test_deprecated_from_import_resolves_to_agent_facade() -> None:
    with pytest.warns(DeprecationWarning, match="vibesys.api.agent"):
        from vibesys.api import agent_projection  # noqa: PLC0415

    assert agent_projection is agent_api.agent_projection


def test_deprecated_request_imports_preserve_identity() -> None:
    from vibesys.api import contracts  # noqa: PLC0415
    from vibesys.loops.legacy_request import LoopKind, RunRequest  # noqa: PLC0415

    for module in (generic_api, contracts):
        with pytest.warns(DeprecationWarning, match="deprecated for new policies"):
            assert getattr(module, "LoopKind") is LoopKind  # noqa: B009
        with pytest.warns(DeprecationWarning, match="deprecated for new policies"):
            assert getattr(module, "RunRequest") is RunRequest  # noqa: B009
        assert {"LoopKind", "RunRequest"} <= set(module.__all__)


@pytest.mark.parametrize(
    "name", ["ExecutableOrchestration", "Orchestration", "OrchestrationRegistry", "RunDescription"]
)
def test_internal_orchestration_imports_remain_deprecated_compatibility(name: str) -> None:
    with pytest.warns(DeprecationWarning, match="vibesys.orchestration.contracts"):
        assert getattr(generic_api, name) is getattr(orchestration_contracts, name)
    assert name not in generic_api.__all__


def test_builtin_registry_import_remains_deprecated_compatibility() -> None:
    with pytest.warns(DeprecationWarning, match="vibesys.loops.registry"):
        assert (
            generic_api.built_in_orchestrations
            is import_module("vibesys.loops.registry").built_in_orchestrations
        )
    assert "built_in_orchestrations" not in generic_api.__all__
