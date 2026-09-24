"""Versioned agent descriptors and early policy validation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vibesys.api.agent import is_agent_run_manifest
from vibesys.errors import ConfigurationError
from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.loops.agent.entrypoint import MultiAgentOrchestrator, SingleAgentOrchestrator
from vibesys.loops.agent.orchestration import (
    AgentOrchestrationOptions,
    UnsupportedAgentOrchestrationError,
    compare_resume_descriptors,
    descriptor_from_options,
    options_from_descriptor,
)
from vs_project.api import (
    OrchestrationDescriptor,
    OrchestrationRunManifest,
    RunEnvironmentRecord,
    RunExecutionRecord,
)


def _options(
    *, max_rounds: int = 4, metric_space: MetricSpace | None = None
) -> AgentOrchestrationOptions:
    return AgentOrchestrationOptions(
        interface="inprocess",
        max_rounds=max_rounds,
        max_retries_per_round=2,
        judge_every=1,
        official_eval_every=1,
        memory_layout="files",
        metric_space=metric_space or MetricSpace(),
    )


@pytest.mark.parametrize("orchestration_id", ["multi-agent", "single-agent"])
def test_agent_descriptor_preserves_policy_and_metric_space(orchestration_id: str) -> None:
    space = MetricSpace(
        objectives=(Objective(name="throughput", direction="max"),), relative_noise=0.01
    )
    descriptor = descriptor_from_options(
        _options(metric_space=space), orchestration_id=orchestration_id
    )

    assert descriptor.id == orchestration_id
    assert descriptor.config_version == 1
    assert "run_environment" not in descriptor.options
    assert "inner_loop" not in descriptor.options
    assert options_from_descriptor(descriptor).metric_space == space
    if orchestration_id == "single-agent":
        assert SingleAgentOrchestrator(descriptor).options.metric_space == space
        with pytest.raises(UnsupportedAgentOrchestrationError):
            MultiAgentOrchestrator(descriptor)


def test_agent_resume_accepts_only_increased_round_limit() -> None:
    recorded = descriptor_from_options(_options(), orchestration_id="multi-agent")
    unchanged = compare_resume_descriptors(recorded, recorded)
    increased = descriptor_from_options(_options(max_rounds=7), orchestration_id="multi-agent")

    assert unchanged.descriptor is None
    assert not unchanged.requires_clean_workspace
    decision = compare_resume_descriptors(recorded, increased)
    assert decision.descriptor == increased
    assert decision.requires_clean_workspace

    with pytest.raises(ConfigurationError, match="cannot decrease"):
        compare_resume_descriptors(increased, recorded)


def test_v4_agent_manifest_is_identified_for_each_strategy() -> None:
    for orchestration_id in ("multi-agent", "single-agent"):
        manifest = OrchestrationRunManifest(
            schema_version=4,
            run_id="run",
            project_id="project",
            display_name="Run",
            created_at=datetime.now(UTC),
            input_fingerprint="0" * 64,
            trusted_input_baseline="0" * 40,
            branch="vibesys-runs/run",
            vibesys_version="test",
            run_environment=RunEnvironmentRecord(name="local"),
            execution=RunExecutionRecord(
                model="test-model",
                agent_backend="stub",
                compute_backend="cpu",
                requested_profiler="none",
                resolved_profiler="none",
            ),
            orchestration=descriptor_from_options(_options(), orchestration_id=orchestration_id),
        )
        assert is_agent_run_manifest(manifest)


def test_agent_resume_rejects_changed_policy_and_unknown_options() -> None:
    recorded = descriptor_from_options(_options(), orchestration_id="multi-agent")
    changed = descriptor_from_options(
        _options(metric_space=MetricSpace(objectives=(Objective("score", "max"),))),
        orchestration_id="multi-agent",
    )
    with pytest.raises(ConfigurationError, match="metric_space"):
        compare_resume_descriptors(recorded, changed)

    unknown = OrchestrationDescriptor(id="multi-agent", config_version=2, options=recorded.options)
    with pytest.raises(UnsupportedAgentOrchestrationError):
        options_from_descriptor(unknown)

    invalid = OrchestrationDescriptor(
        id="multi-agent",
        config_version=1,
        options={**recorded.options, "inner_loop": "single-agent"},
    )
    with pytest.raises(ValidationError, match="inner_loop"):
        options_from_descriptor(invalid)
