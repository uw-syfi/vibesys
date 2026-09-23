"""Versioned agent orchestration descriptor and resume compatibility."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vibesys.api import is_agent_run_manifest
from vibesys.errors import ConfigurationError
from vibesys.loops.agent.orchestration import (
    UnsupportedAgentOrchestrationError,
    compare_resume_descriptors,
    configuration_from_manifest,
    descriptor_from_configuration,
    options_from_descriptor,
)
from vs_project.api import (
    AgentRunConfiguration,
    OrchestrationDescriptor,
    OrchestrationRunManifest,
    RunEnvironmentRecord,
)


def _configuration(
    *, outer_loop: str = "agent", max_rounds: int = 4, objectives: tuple[str, ...] = ()
) -> AgentRunConfiguration:
    return AgentRunConfiguration.model_validate(
        {
            "outer_loop": outer_loop,
            "run_environment": RunEnvironmentRecord(name="local").model_dump(),
            "inner_loop": "agent",
            "interface": "cli",
            "agent_backend": "cli",
            "compute_backend": "local",
            "max_rounds": max_rounds,
            "max_retries_per_round": 2,
            "judge_every": 1,
            "official_eval_every": 1,
            "memory_layout": "split",
            "objectives": objectives,
        }
    )


@pytest.mark.parametrize("outer_loop", ["agent", "profile-guided"])
def test_agent_descriptor_preserves_policy(outer_loop: str) -> None:
    descriptor = descriptor_from_configuration(
        _configuration(outer_loop=outer_loop, objectives=("throughput:max",))
    )

    assert descriptor.id == outer_loop
    assert descriptor.config_version == 1
    assert "run_environment" not in descriptor.options
    assert options_from_descriptor(descriptor).objectives == ("throughput:max",)


def test_agent_resume_accepts_only_increased_round_limit() -> None:
    recorded = descriptor_from_configuration(_configuration())
    unchanged = compare_resume_descriptors(recorded, recorded)
    increased = descriptor_from_configuration(_configuration(max_rounds=7))

    assert unchanged.descriptor is None
    assert not unchanged.requires_clean_workspace
    decision = compare_resume_descriptors(recorded, increased)
    assert decision.descriptor == increased
    assert decision.requires_clean_workspace

    with pytest.raises(ConfigurationError, match="cannot decrease"):
        compare_resume_descriptors(increased, recorded)


def test_v4_agent_manifest_projects_into_cli_resume_contract() -> None:
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
        orchestration=descriptor_from_configuration(
            _configuration(outer_loop="profile-guided", objectives=("score:max",))
        ),
    )

    projected = configuration_from_manifest(manifest)
    assert projected.outer_loop == "profile-guided"
    assert projected.objectives == ("score:max",)
    assert projected.run_environment.name == "local"
    assert not is_agent_run_manifest(manifest)


def test_agent_resume_rejects_changed_policy_and_unknown_version() -> None:
    recorded = descriptor_from_configuration(_configuration())
    changed = descriptor_from_configuration(_configuration(objectives=("score:max",)))
    with pytest.raises(ConfigurationError, match="objectives"):
        compare_resume_descriptors(recorded, changed)

    unknown = OrchestrationDescriptor(id="agent", config_version=2, options=recorded.options)
    with pytest.raises(UnsupportedAgentOrchestrationError):
        options_from_descriptor(unknown)

    invalid_text = OrchestrationDescriptor(
        id="agent",
        config_version=1,
        options={**recorded.options, "inner_loop": ""},
    )
    with pytest.raises(ValidationError, match="inner_loop"):
        options_from_descriptor(invalid_text)
