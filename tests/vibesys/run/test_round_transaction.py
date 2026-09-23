"""Round transactions composed from typed state and real Git repositories."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ConfigDict

from vibesys.run import (
    GitTracker,
    RoundRecoveryOutcome,
    RoundTransactionCoordinator,
    RoundTransactionError,
)
from vibesys.run.agent_round_compat import LegacyAgentRoundStore
from vibesys.run.git_events import NullGitTrackerEvents
from vibesys.run.round_transaction import (
    RoundTransactionCoordinator as GenericRoundTransactionCoordinator,
)
from vs_loop_state.api import RoundRecord
from vs_project.api import (
    AgentRunConfiguration,
    Project,
    ProjectStateError,
    RunEnvironmentRecord,
    StateTransition,
    serialize_round,
)

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "transaction-test"


class _AgentState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    active_hypothesis_id: str | None = None
    completed_rounds: tuple[int, ...] = ()


class _LegacyFixture(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True, strict=True)


def _configuration() -> AgentRunConfiguration:
    return AgentRunConfiguration(
        outer_loop="agent",
        run_environment=RunEnvironmentRecord(name="local"),
        inner_loop="multi-agent",
        interface="inprocess",
        agent_backend="cli",
        compute_backend="cpu",
        max_rounds=5,
        max_retries_per_round=2,
        judge_every=1,
        official_eval_every=1,
        memory_layout="files",
    )


def _project(tmp_path: Path) -> tuple[Project, GitTracker, RoundTransactionCoordinator]:
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
        configuration=_configuration(),
        trusted_input_baseline=tracker.trusted_input_baseline,
        now=datetime(2026, 8, 11, 0, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)
    tracker.snapshot_with_framework_metadata(
        "initialize run",
        project.state.initialization_snapshot(_RUN_ID),
    )
    return (
        project,
        tracker,
        RoundTransactionCoordinator(
            project,
            tracker,
            _RUN_ID,
            agent_state_model_type=_AgentState,
        ),
    )


def _state_slot(project: Project):  # noqa: ANN202
    return project.state.portable_namespace(_RUN_ID, "agent").slot(
        "state.json",
        _AgentState,
    )


def _transition(
    project: Project,
    *,
    active: str | None,
    rounds: tuple[int, ...],
) -> StateTransition:
    return _state_slot(project).transition(
        _AgentState(active_hypothesis_id=active, completed_rounds=rounds)
    )


def _load_state(project: Project) -> _AgentState | None:
    return _state_slot(project).load_optional()


def _restart(
    project: Project,
    tracker: GitTracker,
) -> RoundTransactionCoordinator:
    return RoundTransactionCoordinator(
        project,
        tracker,
        _RUN_ID,
        agent_state_model_type=_AgentState,
    )


def test_complete_commits_candidate_and_exact_typed_agent_state(tmp_path: Path) -> None:
    project, tracker, coordinator = _project(tmp_path)
    transition = _transition(project, active="hypothesis-1", rounds=(1,))

    transaction = coordinator.begin(1, state_transition=transition)
    (tmp_path / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    completed = transaction.complete()

    assert completed.checkpoint == tracker.current_sha()
    assert tracker.run(["git", "show", "HEAD:main.py"]).stdout == b"VALUE = 2\n"
    assert _load_state(project) == _AgentState(
        active_hypothesis_id="hypothesis-1",
        completed_rounds=(1,),
    )
    assert (
        tracker.run(
            [
                "git",
                "show",
                f"HEAD:.vibesys/state/runs/{_RUN_ID}/agent/state.json",
            ]
        ).stdout
        == _state_slot(project).snapshot_transition(transition).files[0].contents
    )
    assert coordinator.recover() is RoundRecoveryOutcome.NO_TRANSACTION


def test_generic_transaction_commits_a_policy_owned_state_slot(tmp_path: Path) -> None:
    project, tracker, _legacy = _project(tmp_path)
    state_slot = project.state.portable_namespace(_RUN_ID, "team-search").slot(
        "state.json", _AgentState
    )
    coordinator = GenericRoundTransactionCoordinator(
        project, tracker, _RUN_ID, state_slot=state_slot
    )
    transition = state_slot.transition(_AgentState(active_hypothesis_id="candidate-1"))

    coordinator.begin(1, state_transition=transition).complete()

    assert state_slot.load_optional() == _AgentState(active_hypothesis_id="candidate-1")
    assert _state_slot(project).load_optional() is None
    assert (
        tracker.run(
            ["git", "show", f"HEAD:.vibesys/state/runs/{_RUN_ID}/team-search/state.json"]
        ).stdout
        == state_slot.snapshot_transition(transition).files[0].contents
    )


def test_recovery_rolls_prepared_state_and_candidate_forward(tmp_path: Path) -> None:
    project, tracker, coordinator = _project(tmp_path)
    (tmp_path / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    coordinator.begin(
        1,
        state_transition=_transition(project, active=None, rounds=(1,)),
    )

    restarted = _restart(project, tracker)

    assert restarted.recover() is RoundRecoveryOutcome.COMMITTED
    assert _load_state(project) == _AgentState(completed_rounds=(1,))
    assert tracker.run(["git", "show", "HEAD:main.py"]).stdout == b"VALUE = 2\n"
    assert restarted.recover() is RoundRecoveryOutcome.NO_TRANSACTION


def test_recovery_restores_an_already_committed_state_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, tracker, coordinator = _project(tmp_path)
    transition = _transition(project, active=None, rounds=(1,))
    transaction = coordinator.begin(1, state_transition=transition)

    original_clear = coordinator._clear_journal  # noqa: SLF001
    monkeypatch.setattr(coordinator, "_clear_journal", lambda: None)
    transaction.complete()
    monkeypatch.setattr(coordinator, "_clear_journal", original_clear)
    _state_slot(project).save(_AgentState(active_hypothesis_id="corrupt"))
    committed_head = tracker.current_sha()

    restarted = _restart(project, tracker)
    assert restarted.recover() is RoundRecoveryOutcome.COMMITTED
    assert tracker.current_sha() == committed_head
    assert _load_state(project) == _AgentState(completed_rounds=(1,))


def test_recovery_translates_corrupt_journal_state(tmp_path: Path) -> None:
    project, _tracker, coordinator = _project(tmp_path)
    journal_directory = project.state.local_namespace(
        _RUN_ID,
        "transaction",
    ).external_directory()
    (journal_directory / "round.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(RoundTransactionError, match="Invalid round transaction journal"):
        coordinator.recover()


def test_begin_translates_corrupt_journal_state(tmp_path: Path) -> None:
    project, _tracker, coordinator = _project(tmp_path)
    journal_directory = project.state.local_namespace(
        _RUN_ID,
        "transaction",
    ).external_directory()
    (journal_directory / "round.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(RoundTransactionError, match="Invalid round transaction journal"):
        coordinator.begin(
            1,
            state_transition=_transition(project, active=None, rounds=(1,)),
        )


def test_snapshot_failure_remains_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, tracker, coordinator = _project(tmp_path)
    transaction = coordinator.begin(
        1,
        state_transition=_transition(project, active="after", rounds=(1,)),
    )
    (tmp_path / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    original_snapshot = tracker.snapshot_with_framework_metadata

    def fail_snapshot(_label: str, _snapshot: object) -> None:
        raise RuntimeError("simulated process failure")  # noqa: TRY003

    monkeypatch.setattr(tracker, "snapshot_with_framework_metadata", fail_snapshot)
    with pytest.raises(RuntimeError, match="simulated process failure"):
        transaction.complete()
    monkeypatch.setattr(tracker, "snapshot_with_framework_metadata", original_snapshot)

    restarted = _restart(project, tracker)
    assert restarted.recover() is RoundRecoveryOutcome.COMMITTED
    assert _load_state(project) == _AgentState(
        active_hypothesis_id="after",
        completed_rounds=(1,),
    )


def test_begin_rejects_staged_changes_without_leaving_a_transaction(tmp_path: Path) -> None:
    project, tracker, coordinator = _project(tmp_path)
    (tmp_path / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    tracker.run(["git", "add", "--", "main.py"])

    with pytest.raises(RoundTransactionError, match="index contains staged changes"):
        coordinator.begin(
            1,
            state_transition=_transition(project, active=None, rounds=(1,)),
        )

    tracker.run(["git", "reset", "--quiet", "HEAD", "--", "."])
    assert coordinator.recover() is RoundRecoveryOutcome.NO_TRANSACTION


def test_begin_rejects_a_transition_from_another_slot(tmp_path: Path) -> None:
    project, _tracker, coordinator = _project(tmp_path)
    wrong_transition = project.state.portable_namespace(_RUN_ID, "plain").transition(
        "cursor.json",
        _AgentState(completed_rounds=(1,)),
    )

    with pytest.raises(RoundTransactionError, match="typed slot"):
        coordinator.begin(1, state_transition=wrong_transition)

    assert coordinator.recover() is RoundRecoveryOutcome.NO_TRANSACTION


def test_begin_rejects_agent_state_deletion(tmp_path: Path) -> None:
    project, _tracker, coordinator = _project(tmp_path)

    with pytest.raises(RoundTransactionError, match="deletion transition"):
        coordinator.begin(1, state_transition=_state_slot(project).transition(None))

    assert coordinator.recover() is RoundRecoveryOutcome.NO_TRANSACTION


def test_transaction_handle_cannot_complete_twice(tmp_path: Path) -> None:
    project, _tracker, coordinator = _project(tmp_path)
    transaction = coordinator.begin(
        1,
        state_transition=_transition(project, active=None, rounds=(1,)),
    )
    transaction.complete()

    with pytest.raises(RoundTransactionError, match="already completed"):
        transaction.complete()


def test_coordinator_requires_matching_run_tracker(tmp_path: Path) -> None:
    project, tracker, _coordinator = _project(tmp_path)
    wrong_run = GitTracker(tmp_path, events=NullGitTrackerEvents(), run_id="another-run")

    with pytest.raises(RoundTransactionError, match="does not match"):
        RoundTransactionCoordinator(
            project,
            wrong_run,
            _RUN_ID,
            agent_state_model_type=_AgentState,
        )

    assert tracker.current_sha() is not None


def test_recovery_accepts_v3_round_and_permissive_active_transition(tmp_path: Path) -> None:
    project, tracker, coordinator = _project(tmp_path)
    record = RoundRecord(
        round_number=1,
        commit=tracker.current_sha(),
        perf_metric=12.5,
        perf_unit="ns/op",
        passed=True,
        hypothesis_id="legacy-hypothesis",
        hypothesis_outcome="proven",
    )
    round_payload = serialize_round(record)
    legacy_active = project.state.local_namespace(_RUN_ID, "agent").transition(
        "active.json",
        _LegacyFixture.model_validate(
            {
                "hypothesis_id": "legacy-hypothesis",
                "nested": {"unknown": [1, 2, 3]},
            },
            strict=True,
        ),
    )
    legacy_slot = project.state.local_namespace(_RUN_ID, "agent").slot(
        "active.json",
        _LegacyFixture,
    )
    active_payload = legacy_slot.serialize_transition(legacy_active)
    journal = {
        "schema_version": 3,
        "run_id": _RUN_ID,
        "round_number": 1,
        "pre_commit": tracker.current_sha(),
        "active_transition_base64": base64.b64encode(active_payload).decode(),
        "round_payload_base64": base64.b64encode(round_payload).decode(),
        "round_payload_sha256": hashlib.sha256(round_payload).hexdigest(),
    }
    journal_path = (
        project.state.local_namespace(
            _RUN_ID,
            "transaction",
        ).external_directory()
        / "round.json"
    )
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    assert coordinator.recover() is RoundRecoveryOutcome.COMMITTED
    assert project.state.load_rounds(_RUN_ID) == [record]
    assert LegacyAgentRoundStore(project, _RUN_ID).load() == [record]
    assert json.loads(
        (
            project.state.local_namespace(_RUN_ID, "agent").external_directory() / "active.json"
        ).read_text(encoding="utf-8")
    ) == {
        "hypothesis_id": "legacy-hypothesis",
        "nested": {"unknown": [1, 2, 3]},
    }


def test_legacy_round_store_preserves_bytes_and_repairs_missing_file(tmp_path: Path) -> None:
    project, tracker, _coordinator = _project(tmp_path)
    record = RoundRecord(
        round_number=1,
        commit=tracker.current_sha(),
        perf_metric=12.5,
        perf_unit="ns/op",
        passed=True,
    )
    rounds = LegacyAgentRoundStore(project, _RUN_ID)
    expected = rounds.prepare_snapshot(record)

    assert rounds.save(record) == expected
    assert rounds.save(record) == expected
    assert rounds.load() == [record]
    namespace = project.state.portable_namespace(_RUN_ID, "agent")
    assert namespace.read_bytes("rounds/0001.json") == serialize_round(record)

    namespace.delete("rounds/0001.json")
    assert rounds.restore(record) == expected
    assert rounds.load() == [record]


def test_legacy_round_store_rejects_unexpected_directory(tmp_path: Path) -> None:
    project, _tracker, _coordinator = _project(tmp_path)
    project.state.portable_namespace(_RUN_ID, "agent").external_directory("rounds/extra")

    with pytest.raises(ProjectStateError, match="Unexpected completed-round entry"):
        LegacyAgentRoundStore(project, _RUN_ID).load()


def test_legacy_round_store_rejects_symlinked_round_file(tmp_path: Path) -> None:
    project, _tracker, _coordinator = _project(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text('{"round":1}', encoding="utf-8")
    rounds_directory = project.state.portable_namespace(_RUN_ID, "agent").external_directory(
        "rounds"
    )
    (rounds_directory / "0001.json").symlink_to(outside)

    with pytest.raises(ProjectStateError, match="symlinks"):
        LegacyAgentRoundStore(project, _RUN_ID).load()
