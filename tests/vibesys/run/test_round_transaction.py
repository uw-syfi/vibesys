"""Checkpoint transactions composed from typed state and real Git repositories."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ConfigDict
from tests.support.run_execution import run_execution_record

from vibesys.run import GitTracker, RoundRecoveryOutcome, RoundTransactionError
from vibesys.run.git_events import NullGitTrackerEvents
from vibesys.run.round_transaction import MultiSlotRoundTransactionCoordinator
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "transaction-test"


class _AgentState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    active_hypothesis_id: str | None = None
    completed_rounds: tuple[int, ...] = ()


def _coordinator(
    project: Project, tracker: GitTracker, *, namespace: str = "agent"
) -> MultiSlotRoundTransactionCoordinator:
    return MultiSlotRoundTransactionCoordinator(
        project,
        tracker,
        _RUN_ID,
        namespace=namespace,
        models={"state.json": _AgentState},
    )


def _project(tmp_path: Path) -> tuple[Project, GitTracker, MultiSlotRoundTransactionCoordinator]:
    (tmp_path / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    project = Project.open(tmp_path)
    project.state.create_project("transaction test", now=datetime(2026, 8, 11, tzinfo=UTC))
    tracker = GitTracker(tmp_path, events=NullGitTrackerEvents(), run_id=_RUN_ID)
    tracker.init(existing=False)
    assert tracker.trusted_input_baseline is not None
    assert tracker.project_branch is not None
    manifest = project.state.new_run_manifest(
        "transaction test",
        run_id=_RUN_ID,
        branch=tracker.project_branch,
        vibesys_version="0.1.0",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="multi-agent", config_version=1, options={}),
        trusted_input_baseline=tracker.trusted_input_baseline,
        now=datetime(2026, 8, 11, 0, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)
    tracker.snapshot_with_framework_metadata(
        "initialize run", project.state.initialization_snapshot(_RUN_ID)
    )
    return project, tracker, _coordinator(project, tracker)


def _state_slot(project: Project):  # noqa: ANN202
    return project.state.portable_namespace(_RUN_ID, "agent").slot("state.json", _AgentState)


def _state(*, active: str | None, rounds: tuple[int, ...]) -> _AgentState:
    return _AgentState(active_hypothesis_id=active, completed_rounds=rounds)


def _load_state(project: Project) -> _AgentState | None:
    return _state_slot(project).load_optional()


def test_complete_commits_candidate_and_exact_typed_agent_state(tmp_path: Path) -> None:
    project, tracker, coordinator = _project(tmp_path)
    state = _state(active="hypothesis-1", rounds=(1,))

    transaction = coordinator.begin(1, writes={"state.json": state})
    (tmp_path / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    completed = transaction.complete()

    assert completed.checkpoint == tracker.current_sha()
    assert tracker.run(["git", "show", "HEAD:main.py"]).stdout == b"VALUE = 2\n"
    assert _load_state(project) == state
    assert (
        tracker.run(["git", "show", f"HEAD:.vibesys/state/runs/{_RUN_ID}/agent/state.json"]).stdout
        == _state_slot(project)
        .snapshot_transition(_state_slot(project).transition(state))
        .files[0]
        .contents
    )
    assert coordinator.recover() is RoundRecoveryOutcome.NO_TRANSACTION


def test_generic_transaction_commits_a_policy_owned_state_slot(tmp_path: Path) -> None:
    project, tracker, _ = _project(tmp_path)
    state_slot = project.state.portable_namespace(_RUN_ID, "team-search").slot(
        "state.json", _AgentState
    )
    coordinator = _coordinator(project, tracker, namespace="team-search")
    state = _AgentState(active_hypothesis_id="candidate-1")

    coordinator.begin(1, writes={"state.json": state}).complete()

    assert state_slot.load_optional() == state
    assert _state_slot(project).load_optional() is None
    assert (
        tracker.run(
            ["git", "show", f"HEAD:.vibesys/state/runs/{_RUN_ID}/team-search/state.json"]
        ).stdout
        == state_slot.snapshot_transition(state_slot.transition(state)).files[0].contents
    )


def test_recovery_rolls_prepared_state_and_candidate_forward(tmp_path: Path) -> None:
    project, tracker, coordinator = _project(tmp_path)
    (tmp_path / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    coordinator.begin(1, writes={"state.json": _state(active=None, rounds=(1,))})

    restarted = _coordinator(project, tracker)

    assert restarted.recover() is RoundRecoveryOutcome.COMMITTED
    assert _load_state(project) == _AgentState(completed_rounds=(1,))
    assert tracker.run(["git", "show", "HEAD:main.py"]).stdout == b"VALUE = 2\n"
    assert restarted.recover() is RoundRecoveryOutcome.NO_TRANSACTION


def test_recovery_restores_an_already_committed_state_file(tmp_path: Path) -> None:
    project, tracker, coordinator = _project(tmp_path)
    transaction = coordinator.begin(1, writes={"state.json": _state(active=None, rounds=(1,))})
    journal = coordinator._journal_slot.load_optional()  # noqa: SLF001
    assert journal is not None
    transaction.complete()
    coordinator._journal_slot.save(journal)  # noqa: SLF001
    _state_slot(project).save(_AgentState(active_hypothesis_id="corrupt"))
    committed_head = tracker.current_sha()

    restarted = _coordinator(project, tracker)
    assert restarted.recover() is RoundRecoveryOutcome.COMMITTED
    assert tracker.current_sha() == committed_head
    assert _load_state(project) == _AgentState(completed_rounds=(1,))


def test_recovery_translates_corrupt_journal_state(tmp_path: Path) -> None:
    project, _tracker, coordinator = _project(tmp_path)
    journal_directory = project.state.local_namespace(_RUN_ID, "transaction").external_directory()
    (journal_directory / "checkpoint.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(RoundTransactionError, match="Invalid checkpoint journal"):
        coordinator.recover()


def test_begin_translates_corrupt_journal_state(tmp_path: Path) -> None:
    project, _tracker, coordinator = _project(tmp_path)
    journal_directory = project.state.local_namespace(_RUN_ID, "transaction").external_directory()
    (journal_directory / "checkpoint.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(RoundTransactionError, match="Invalid checkpoint journal"):
        coordinator.begin(1, writes={"state.json": _state(active=None, rounds=(1,))})


def test_snapshot_failure_remains_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tracker, coordinator = _project(tmp_path)
    transaction = coordinator.begin(1, writes={"state.json": _state(active="after", rounds=(1,))})
    (tmp_path / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    original_snapshot = tracker.snapshot_with_framework_metadata

    def fail_snapshot(_label: str, _snapshot: object) -> None:
        raise RuntimeError("simulated process failure")  # noqa: TRY003

    monkeypatch.setattr(tracker, "snapshot_with_framework_metadata", fail_snapshot)
    with pytest.raises(RuntimeError, match="simulated process failure"):
        transaction.complete()
    monkeypatch.setattr(tracker, "snapshot_with_framework_metadata", original_snapshot)

    restarted = _coordinator(project, tracker)
    assert restarted.recover() is RoundRecoveryOutcome.COMMITTED
    assert _load_state(project) == _state(active="after", rounds=(1,))


def test_begin_rejects_staged_changes_without_leaving_a_transaction(tmp_path: Path) -> None:
    _project_data, tracker, coordinator = _project(tmp_path)
    (tmp_path / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    tracker.run(["git", "add", "--", "main.py"])

    with pytest.raises(RoundTransactionError, match="index has staged changes"):
        coordinator.begin(1, writes={"state.json": _state(active=None, rounds=(1,))})

    tracker.run(["git", "reset", "--quiet", "HEAD", "--", "."])
    assert coordinator.recover() is RoundRecoveryOutcome.NO_TRANSACTION


def test_begin_rejects_a_transition_from_another_slot(tmp_path: Path) -> None:
    _project_data, _tracker, coordinator = _project(tmp_path)

    with pytest.raises(RoundTransactionError, match="Undeclared checkpoint slot"):
        coordinator.begin(1, writes={"other.json": _state(active=None, rounds=(1,))})

    assert coordinator.recover() is RoundRecoveryOutcome.NO_TRANSACTION


def test_transaction_handle_cannot_complete_twice(tmp_path: Path) -> None:
    _project_data, _tracker, coordinator = _project(tmp_path)
    transaction = coordinator.begin(1, writes={"state.json": _state(active=None, rounds=(1,))})
    transaction.complete()

    with pytest.raises(RoundTransactionError, match="already completed"):
        transaction.complete()


def test_coordinator_requires_matching_run_tracker(tmp_path: Path) -> None:
    project, tracker, _ = _project(tmp_path)
    wrong_run = GitTracker(tmp_path, events=NullGitTrackerEvents(), run_id="another-run")

    with pytest.raises(RoundTransactionError, match="must agree"):
        _coordinator(project, wrong_run)

    assert tracker.current_sha() is not None
