"""Filesystem contract tests for the plain-loop state adapter."""

from datetime import UTC, datetime

import pytest
from tests.support.run_execution import run_execution_record

from vibesys.loops.plain.orchestration import PlainOrchestrationOptions, descriptor_from_options
from vibesys.loops.plain.state import PlainStateStore
from vs_loop_state.api import PlainLoopCursor, PlainPerformanceRecord
from vs_project.api import (
    Project,
    ProjectStateError,
    RunEnvironmentRecord,
)


def _store(tmp_path) -> PlainStateStore:  # noqa: ANN001
    project = Project.open(tmp_path)
    project.state.create_project("test")
    run = project.state.new_run_manifest(
        "test",
        run_id="run-1",
        branch="vibesys/run-1",
        vibesys_version="test",
        trusted_input_baseline="a" * 40,
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=descriptor_from_options(
            PlainOrchestrationOptions(
                max_rounds=1,
                max_attempts_per_issue=1,
                max_issues_per_perf_eval=1,
            ),
        ),
    )
    project.state.create_run(run)
    namespace = project.state.portable_namespace("run-1", "plain")
    return PlainStateStore(namespace)


def test_plain_state_store_round_trips_typed_state(tmp_path) -> None:  # noqa: ANN001
    store = _store(tmp_path)
    cursor = PlainLoopCursor(
        round_idx=2,
        phase="judge",
        current_issue_id=5,
        bootstrap_done=True,
    )
    performance = PlainPerformanceRecord(
        iteration=1,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        throughput_trend="improved",
        latency_trend="mixed",
        metrics={"throughput": 42.0},
        new_issue_ids=(3,),
    )

    store.save_cursor(cursor)
    store.append_performance(performance)

    assert store.load_cursor() == cursor
    assert store.load_performance().records == (performance,)


def test_plain_state_store_distinguishes_missing_from_corrupt(tmp_path) -> None:  # noqa: ANN001
    store = _store(tmp_path)
    assert store.load_cursor() is None

    path = store.namespace.external_directory() / "state.json"
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(ProjectStateError, match=r"state\.json"):
        store.load_cursor()
