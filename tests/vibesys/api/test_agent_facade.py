"""The generic API stays independent of agent policy projection imports."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime

import vibesys.api as generic_api
from vibesys.api import agent as agent_api
from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.orchestrations.single.models import SingleOptions
from vs_project.api import (
    OrchestrationDescriptor,
    OrchestrationRunManifest,
    RunEnvironmentRecord,
    RunExecutionRecord,
)


def _manifest(orchestration: OrchestrationDescriptor) -> OrchestrationRunManifest:
    return OrchestrationRunManifest(
        schema_version=5,
        run_id="run-1",
        project_id="project-1",
        display_name="Run 1",
        created_at=datetime(2026, 9, 27, tzinfo=UTC),
        input_fingerprint="a" * 64,
        trusted_input_baseline="b" * 40,
        branch="vibesys/run-1",
        vibesys_version="0.2.0",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=RunExecutionRecord(
            model="test-model",
            agent_backend="stub",
            compute_backend="cpu",
            requested_profiler="none",
            resolved_profiler="none",
        ),
        orchestration=orchestration,
    )


def test_generic_api_import_does_not_load_builtin_policies() -> None:
    script = (
        "import sys, vibesys.api; "
        "assert 'vibesys.plugin_catalog' not in sys.modules; "
        "assert not any(name.startswith(('vibesys.orchestrations.multi', "
        "'vibesys.orchestrations.single')) for name in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", script], check=True)  # noqa: S603  # LW-030001; The subprocess runs the current interpreter on a fixed script literal.


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


def test_agent_objectives_are_parsed_by_the_registered_plugin() -> None:
    options = SingleOptions(
        interface="inprocess",
        max_rounds=3,
        max_retries_per_round=1,
        judge_every=1,
        official_eval_every=1,
        memory_layout="files",
        metric_space=MetricSpace(
            objectives=(
                Objective("throughput", "max"),
                Objective("latency", "min"),
            )
        ),
    )
    manifest = _manifest(
        OrchestrationDescriptor(
            id="single-agent",
            config_version=1,
            options=options.model_dump(mode="json"),
        )
    )

    assert agent_api.agent_run_objectives(manifest) == (
        "throughput:max",
        "latency:min",
    )
