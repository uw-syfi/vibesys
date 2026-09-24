"""Agent-only API names stay available without loading them for generic imports."""

from __future__ import annotations

import subprocess
import sys

import pytest

import vibesys.api as generic_api
from vibesys.api import agent as agent_api


def test_generic_api_import_does_not_load_agent_projection() -> None:
    script = (
        "import sys, vibesys.api; "
        "assert 'vibesys.api._orchestrations.agent_projection' not in sys.modules; "
        "assert 'vibesys.api._orchestrations.builtins' not in sys.modules; "
        "assert 'vibesys.loops.agent.issue_board' not in sys.modules"
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
