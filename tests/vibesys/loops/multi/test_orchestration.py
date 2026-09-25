"""Versioned agent descriptors and early policy validation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vibesys.api.agent import is_agent_run_manifest
from vibesys.errors import ConfigurationError, InvalidStrategyOptionsError
from vibesys.evaluators.input_manifest import ProfileGuidedInput
from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.loops.agent_options import (
    AgentOrchestrationOptions,
    UnsupportedAgentOrchestrationError,
    compare_resume_descriptors,
    descriptor_from_options,
    options_from_descriptor,
)
from vibesys.loops.multi.orchestration import (
    MultiAgentOrchestrator,
    ProfileGuidedMultiAgentOrchestrator,
    load_profile_options,
)
from vibesys.loops.single.orchestration import (
    ProfileGuidedSingleAgentOrchestrator,
    SingleAgentOrchestrator,
    SingleProjector,
)
from vibesys.orchestration.view import RunStatus
from vibesys.search.hypothesis.state import HypothesisState
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


def _profile_options() -> AgentOrchestrationOptions:
    return _options().model_copy(
        update={"profile_guided": ProfileGuidedInput(command=("python", "attribute.py"))}
    )


def test_profile_multi_preset_requires_profile_guided_options() -> None:
    """The profile-guided-multi preset validates the same option shape as
    before: ``profile_guided`` is required, and the plain descriptor is
    rejected.
    """
    preset_id = "profile-guided-multi-agent"
    missing = descriptor_from_options(_options(), orchestration_id=preset_id)
    with pytest.raises(InvalidStrategyOptionsError, match="profile_guided"):
        load_profile_options(missing)
    with pytest.raises(InvalidStrategyOptionsError, match="profile_guided"):
        ProfileGuidedMultiAgentOrchestrator(missing)

    present = descriptor_from_options(_profile_options(), orchestration_id=preset_id)
    validated = load_profile_options(present)
    assert validated.profile_guided is not None
    assert ProfileGuidedMultiAgentOrchestrator(present).options.profile_guided is not None


def test_plain_strategies_reject_profile_guided_options() -> None:
    """Plain ``multi-agent``/``single-agent`` forbid ``profile_guided``: it
    is only valid on their profile-guided presets.
    """
    for orchestration_id, orchestrator_cls in (
        ("multi-agent", MultiAgentOrchestrator),
        ("single-agent", SingleAgentOrchestrator),
    ):
        descriptor = descriptor_from_options(_profile_options(), orchestration_id=orchestration_id)
        with pytest.raises(InvalidStrategyOptionsError, match="profile_guided"):
            orchestrator_cls(descriptor)


def test_profile_single_preset_requires_profile_guided_options() -> None:
    descriptor = descriptor_from_options(_options(), orchestration_id="profile-guided-single-agent")
    with pytest.raises(InvalidStrategyOptionsError, match="profile_guided"):
        ProfileGuidedSingleAgentOrchestrator(descriptor)

    present = descriptor_from_options(
        _profile_options(), orchestration_id="profile-guided-single-agent"
    )
    assert ProfileGuidedSingleAgentOrchestrator(present).options.profile_guided is not None


@pytest.mark.parametrize(
    ("orchestration_id", "orchestrator_cls"),
    [
        ("multi-agent", MultiAgentOrchestrator),
        ("single-agent", SingleAgentOrchestrator),
    ],
)
def test_orchestrator_rejects_mismatched_descriptor_id(
    orchestration_id: str, orchestrator_cls: type
) -> None:
    other = "single-agent" if orchestration_id == "multi-agent" else "multi-agent"
    descriptor = descriptor_from_options(_options(), orchestration_id=other)
    with pytest.raises(UnsupportedAgentOrchestrationError):
        orchestrator_cls(descriptor)


@pytest.mark.parametrize(
    ("orchestration_id", "orchestrator_cls"),
    [
        ("multi-agent", MultiAgentOrchestrator),
        ("single-agent", SingleAgentOrchestrator),
    ],
)
@pytest.mark.parametrize(
    ("field", "value"),
    [("interface", "native"), ("memory_layout", "bogus")],
)
def test_orchestrator_rejects_invalid_settings(
    orchestration_id: str, orchestrator_cls: type, field: str, value: str
) -> None:
    options = _options().model_copy(update={field: value})
    descriptor = descriptor_from_options(options, orchestration_id=orchestration_id)
    with pytest.raises(InvalidStrategyOptionsError, match=field):
        orchestrator_cls(descriptor)


def test_single_projector_views_committed_and_persisted_state() -> None:
    """``SingleProjector`` reads a just-committed state and a persisted one
    the same way ``MultiProjector`` does, and ignores a foreign namespace.
    """
    projector = SingleProjector()
    state = HypothesisState()

    committed = projector.project_committed("single", state, run_id="run-1")
    assert committed is not None
    assert committed.status == RunStatus.ACTIVE

    assert projector.project_committed("other-namespace", state, run_id="run-1") is None
