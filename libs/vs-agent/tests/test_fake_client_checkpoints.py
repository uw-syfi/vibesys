"""Fake raw turns adopt the same durable provider checkpoint as production."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from vs_agent.api import (
    AgentCapabilities,
    AgentExecutionPolicy,
    AgentSessionKey,
    AgentSessionSpec,
    AgentSessionState,
    AgentTurnRequest,
    DurableSessionStore,
    SessionResumeError,
    SkillSelection,
)
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    RunExecutionRecord,
)

if TYPE_CHECKING:
    from pathlib import Path


def _checkpoint_store(tmp_path: Path) -> DurableSessionStore:
    project = Project.open(tmp_path)
    project.state.create_project("checkpoint")
    manifest = project.state.new_run_manifest(
        "checkpoint",
        run_id="checkpoint",
        branch="test",
        vibesys_version="test",
        trusted_input_baseline="a" * 40,
        run_environment=RunEnvironmentRecord(name="local"),
        execution=RunExecutionRecord(
            model="test",
            agent_backend="stub",
            compute_backend="cpu",
            requested_profiler="none",
            resolved_profiler="none",
            agent_roles={},
        ),
        orchestration=OrchestrationDescriptor(id="test", config_version=1, options={}),
    )
    project.state.create_run(manifest)
    return DurableSessionStore(
        project.state.local_namespace("checkpoint", "agent").slot(
            "sessions.json", AgentSessionState
        )
    )


def test_a_reconstructed_fake_adopts_only_the_matching_completed_session(tmp_path: Path) -> None:
    store = _checkpoint_store(tmp_path)
    capabilities = AgentCapabilities(session_reuse=True, provider_session_resume=True)
    first = FakeAgentClient(capabilities=capabilities, session_store=store)
    spec = AgentSessionSpec(
        role="implementer",
        provider="fake",
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    key = AgentSessionKey.for_member("implementer", "held")
    accepted = first.run(
        session_spec=spec, turn=AgentTurnRequest(message="submit"), session_key=key
    )
    first.close()
    second = FakeAgentClient(capabilities=capabilities, session_store=store)
    resume = AgentTurnRequest(
        message="settled", expected_provider_session_id=accepted.provider_session_id
    )

    completed = second.run(session_spec=spec, turn=resume, session_key=key)

    assert completed.provider_session_id == accepted.provider_session_id
    assert len(second.calls) == 1
    third = FakeAgentClient(capabilities=capabilities, session_store=store)
    with pytest.raises(SessionResumeError, match="specification changed"):
        third.run(session_spec=replace(spec, model="changed"), turn=resume, session_key=key)
    assert third.calls == []


@pytest.mark.parametrize("excluded", [False, True])
def test_raw_fake_turns_materialize_declared_skill_resources(
    tmp_path: Path, *, excluded: bool
) -> None:
    skill = tmp_path / "source" / "policy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: policy\ndescription: Preserve accuracy.\n---\n# Policy\n", encoding="utf-8"
    )
    (skill / "floor.md").write_text("Preserve accuracy.\n", encoding="utf-8")
    (skill / "omitted").mkdir()
    (skill / "omitted" / "private.md").write_text("backend-only resource", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    selection = SkillSelection(
        skip_dir=lambda _directory, names: {"omitted"} if excluded and "omitted" in names else set()
    )
    client = FakeAgentClient(skill_selection=selection)
    spec = AgentSessionSpec(
        role="implementer",
        provider="fake",
        workspace=workspace,
        policy=AgentExecutionPolicy(require_enforcement=False),
        skills=(skill,),
    )

    client.run(session_spec=spec, turn=AgentTurnRequest(message="inspect"))

    assert (workspace / ".agents" / "skills" / "policy" / "floor.md").read_text(
        encoding="utf-8"
    ) == "Preserve accuracy.\n"
    assert (
        workspace / ".agents" / "skills" / "policy" / "omitted" / "private.md"
    ).exists() is not excluded


@pytest.mark.parametrize("reconstruct", [False, True])
def test_changed_spec_replaces_durable_fake_conversation(
    tmp_path: Path, *, reconstruct: bool
) -> None:
    store = _checkpoint_store(tmp_path)
    capabilities = AgentCapabilities(session_reuse=True, provider_session_resume=True)
    client = FakeAgentClient(capabilities=capabilities, session_store=store)
    spec = AgentSessionSpec(
        role="implementer",
        provider="fake",
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    key = AgentSessionKey.for_member("implementer", "held")
    first = client.run(session_spec=spec, turn=AgentTurnRequest(message="first"), session_key=key)
    if reconstruct:
        client.close()
        client = FakeAgentClient(capabilities=capabilities, session_store=store)

    changed = client.run(
        session_spec=replace(spec, model="changed"),
        turn=AgentTurnRequest(message="changed"),
        session_key=key,
    )
    peer = client.run(
        session_spec=spec,
        turn=AgentTurnRequest(message="peer"),
        session_key=AgentSessionKey.for_member("implementer", "peer"),
    )

    assert changed.provider_session_id != first.provider_session_id
    assert peer.provider_session_id != first.provider_session_id
    assert peer.provider_session_id != changed.provider_session_id
