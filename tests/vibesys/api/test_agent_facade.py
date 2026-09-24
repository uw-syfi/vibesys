"""The generic API stays independent of agent policy projection imports."""

from __future__ import annotations

import subprocess
import sys

import vibesys.api as generic_api
from vibesys.api import agent as agent_api


def test_generic_api_import_does_not_load_builtin_policies() -> None:
    script = (
        "import sys, vibesys.api; "
        "assert 'vibesys.loops.registry' not in sys.modules; "
        "assert not any(name.startswith(('vibesys.loops.multi', "
        "'vibesys.loops.single', 'vibesys.loops.profile_multi', "
        "'vibesys.loops.profile_single', 'vibesys.loops.issue_queue', "
        "'vibesys.loops.evolve')) for name in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", script], check=True)  # noqa: S603


def test_agent_projection_names_are_explicitly_owned_by_agent_facade() -> None:
    expected = {
        "AgentRunProjection",
        "HypothesisRoundView",
        "HypothesisView",
        "RoundView",
        "agent_projection",
        "agent_run_objectives",
        "framework_memory_paths",
        "is_agent_run_manifest",
    }
    assert expected <= set(agent_api.__all__)
    assert expected.isdisjoint(generic_api.__all__)
    for name in expected:
        assert getattr(agent_api, name) is not None
