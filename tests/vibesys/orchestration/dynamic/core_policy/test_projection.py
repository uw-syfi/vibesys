"""The dynamic projector shows only recorded, official facts, from live state or the stored record."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, JsonValue
from tests.support.run_execution import run_execution_record
from tests.vibesys.orchestration.dynamic.strategy._harness import envelope

from vibesys.dynamic_core import dynamic_projector
from vibesys.hypothesis.readmodel import agent_projection
from vibesys.orchestration.dynamic.core_policy.api import project_strategy_state
from vibesys.orchestration.dynamic.strategy.api import (
    DynamicStrategyState,
    HypothesisRecord,
    MetricRow,
    RoundRecord,
)
from vibesys.plugin_registration import OrchestrationRegistration, project_run
from vibesys.run.contracts import RunStatus, RunView
from vs_core.api import AttemptId, EventCursor, HostFence, HostId, RunEnvelope
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord, StoredEnvelope
from vs_runtime.api import OrchestrationPlugin, Run
from vs_runtime.api import RunStatus as RuntimeStatus
from vs_runtime.api.core import RuntimeRecord

if TYPE_CHECKING:
    from pathlib import Path


class _Options(BaseModel):
    pass


async def _never(_run: Run, _options: BaseModel) -> RuntimeStatus:
    raise AssertionError


_row = st.builds(
    MetricRow,
    name=st.just("throughput"),
    value=st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False),
    direction=st.just("max"),
    unit=st.just("tok/s"),
)
_metrics = st.lists(_row, max_size=2).map(tuple)


@st.composite
def _states(draw: st.DrawFn) -> DynamicStrategyState:
    counts = draw(st.lists(st.integers(0, 3), max_size=4))
    sequence = 0
    hypotheses = []
    for index, count in enumerate(counts):
        rounds = []
        first = sequence + 1
        for _ in range(count):
            sequence += 1
            rounds.append(
                RoundRecord(
                    sequence=sequence,
                    attempt=AttemptId(root=f"attempt-{sequence}"),
                    review_passed=draw(st.none() | st.booleans()),
                    metrics=draw(_metrics),
                    failure=draw(st.none() | st.just("boom")),
                )
            )
        hypotheses.append(
            HypothesisRecord(
                hypothesis_id=f"h{index}",
                title=f"title {index}",
                hypothesis="claim",
                first_sequence=first,
                rounds=tuple(rounds),
            )
        )
    return DynamicStrategyState(hypotheses=tuple(hypotheses))


@settings(max_examples=60, deadline=None)
@given(_states(), st.integers(0, 50))
def test_projection_copies_recorded_rounds_and_only_measured_numbers(
    state: DynamicStrategyState, revision: int
) -> None:
    projection = project_strategy_state(state, experiment_revision=revision)
    view = agent_projection(_run_view(projection.payload))
    recorded = [record for item in state.hypotheses for record in item.rounds]

    assert view is not None
    assert view.experiment_revision == revision == projection.experiment_revision
    assert view.current_round == len(recorded) == len(projection.rounds)
    assert [item.round_number for item in view.rounds] == sorted(r.sequence for r in recorded)
    by_sequence = {record.sequence: record for record in recorded}
    for item in view.rounds:
        record = by_sequence[item.round_number]
        measured = bool(record.metrics)
        assert item.official_evaluation is measured
        assert (item.perf_metric is not None) is measured
        if measured:
            assert item.perf_metric == record.metrics[0].value
        assert (
            item.judge_verdict
            == {None: "skipped", True: "pass", False: "fail"}[record.review_passed]
        )
    assert [item.hypothesis_id for item in view.hypotheses] == [
        item.hypothesis_id for item in state.hypotheses
    ]


def _run_view(payload: dict[str, JsonValue] | None) -> RunView:
    return RunView(run_id="r", loop="dynamic", status=RunStatus.ACTIVE, projection=payload)


def _project_with_run(root: Path) -> tuple[Project, str]:
    project = Project.open(root)
    project.state.create_project("projection", now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "projection",
        branch="test",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="dynamic", config_version=1, options={}),
        trusted_input_baseline="a" * 40,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)
    return project, manifest.run_id


def _commit(
    project: Project, run_id: str, state: DynamicStrategyState
) -> RuntimeRecord[DynamicStrategyState]:
    _, saved = envelope(state)
    record = RuntimeRecord[DynamicStrategyState].fresh(
        RunEnvelope[DynamicStrategyState](
            schema_version=saved.schema_version,
            fence=HostFence(host_id=HostId(root="writer"), epoch=1),
            strategy_id=saved.strategy_id,
            state_schema=saved.state_schema,
            core=saved.core,
            strategy=state,
            event_cursor=EventCursor(sequence=0),
        )
    )
    store = project.state_store(run_id)
    fence = store.acquire("writer", now=0, duration=1)
    assert fence is not None
    store.commit(
        None,
        StoredEnvelope(revision=0, schema_version=1, payload=record.model_dump_json().encode()),
        fence,
        now=0,
    )
    return record


def _state() -> DynamicStrategyState:
    round_ = RoundRecord(
        sequence=1,
        attempt=AttemptId(root="attempt-1"),
        review_passed=True,
        metrics=(MetricRow(name="throughput", value=81.5, direction="max", unit="tok/s"),),
    )
    return DynamicStrategyState(
        hypotheses=(
            HypothesisRecord(
                hypothesis_id="fuse",
                title="Fuse",
                hypothesis="fusing helps",
                first_sequence=1,
                rounds=(round_,),
            ),
        )
    )


def test_a_run_without_a_record_has_identity_and_status_only_and_stays_unopened(
    tmp_path: Path,
) -> None:
    project, run_id = _project_with_run(tmp_path)

    view = dynamic_projector().view(project, run_id, status=RunStatus.ACTIVE, loop="dynamic")

    assert (view.run_id, view.status, view.projection, view.rounds) == (
        run_id,
        RunStatus.ACTIVE,
        None,
        (),
    )
    assert project.state.state_store_namespace(run_id).read_bytes("store.json") is None


def test_the_stored_record_projects_at_its_core_revision(tmp_path: Path) -> None:
    project, run_id = _project_with_run(tmp_path)
    record = _commit(project, run_id, _state())

    view = dynamic_projector().view(project, run_id, status=RunStatus.COMPLETED, loop="dynamic")

    expected = project_strategy_state(_state(), experiment_revision=record.envelope.revision)
    assert view.projection == expected.payload
    assert view.rounds == expected.rounds
    assert view.experiment_revision == record.envelope.revision
    assert view.status is RunStatus.COMPLETED


def test_a_just_committed_record_projects_without_reading_disk(tmp_path: Path) -> None:
    project, run_id = _project_with_run(tmp_path)
    record = _commit(project, run_id, _state())
    projector = dynamic_projector()

    view = projector.project_committed("dynamic", record, run_id=run_id)

    assert view == projector.view(project, run_id, status=RunStatus.ACTIVE, loop="dynamic")
    assert projector.project_committed("another-plugin", record, run_id=run_id) is None
    assert projector.project_committed("dynamic", _state(), run_id=run_id) is None


def test_a_plugin_without_a_state_model_registers_the_projector(tmp_path: Path) -> None:
    project, run_id = _project_with_run(tmp_path)
    _commit(project, run_id, _state())
    plugin = OrchestrationPlugin(id="dynamic", agents=(), options=_Options, orchestrate=_never)

    registration = OrchestrationRegistration(plugin=plugin, projector=dynamic_projector())
    view = project_run(
        registration, project, run_id=run_id, status=RunStatus.ACTIVE, loop="dynamic"
    )

    assert agent_projection(view) is not None
    assert [item.number for item in view.rounds] == [1]
