"""Generic and policy-specific public facade boundaries."""

from __future__ import annotations

import subprocess
import sys

import vibesys.api as generic_api
from vibesys.api import hypothesis as hypothesis_api


def test_generic_api_import_does_not_load_builtin_policy() -> None:
    script = (
        "import sys, vibesys.api; "
        "assert 'vs_loop_state' not in sys.modules; "
        "assert not any(name.startswith('vibesys.orchestration') for name in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", script], check=True)  # noqa: S603  # LW-030001; the subprocess runs the current interpreter on a fixed script literal.


def test_generic_api_does_not_publish_hypothesis_policy() -> None:
    policy_names = {
        "AgentRunProjection",
        "CandidateDisposition",
        "HypothesisOutcome",
        "HypothesisResolution",
        "HypothesisRoundView",
        "HypothesisView",
        "JudgeVerdict",
        "MetricSpace",
        "Objective",
        "PerfDeltaReason",
        "agent_projection",
        "resolve_openevolve_options",
    }

    assert policy_names.isdisjoint(generic_api.__all__)


def test_hypothesis_policy_has_an_explicit_opt_in_facade() -> None:
    assert {
        "AgentRunProjection",
        "HypothesisRoundView",
        "HypothesisView",
        "agent_projection",
    } <= set(hypothesis_api.__all__)
