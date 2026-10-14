"""The dynamic projector shows only recorded, official facts, from the published record.

The search publishes a `RunSnapshot`; `project_run_snapshot` turns it into the iteration tree,
the rounds and the agent projection frontends read. Its properties are stated over generated
snapshots: one workstream node per scheduled workstream in the order it was scheduled, a
status that follows how it ended, the leader marked kept, and a metric shown only when a
trusted evaluation measured it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, JsonValue
from tests.support.core_plugins import failing_plugin
from tests.vibesys.orchestration.dynamic._host import begin, config

from vibesys.candidates.api import InputReading, Measured, NotConfigured
from vibesys.dynamic_core import dynamic_projector
from vibesys.hypothesis.readmodel import agent_projection
from vibesys.metrics import MetricRow
from vibesys.orchestration.dynamic.core_policy.api import project_run_snapshot
from vibesys.orchestration.dynamic.search.api import (
    AgentName,
    Failure,
    FailureKind,
    FinishedWork,
    HypothesisRecord,
    RoundRecord,
    Running,
    RunSnapshot,
    WorkKind,
    WorkPlan,
    dynamic,
)
from vibesys.plugin_registration import OrchestrationRegistration, project_run
from vibesys.run.contracts import (
    IterationKind,
    IterationStatus,
    IterationTree,
    RunStatus,
    RunView,
)
from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    AttemptId,
    EventCursor,
    FlowState,
    HostFence,
    HostId,
    RevisionRef,
    RunEnvelope,
)
from vs_core.testing.builders import initial_state
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord, StoredEnvelope
from vs_project.api.testing import run_execution_record
from vs_runtime.api.core import RuntimeRecord
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from pathlib import Path


class _Options(BaseModel):
    pass


_PARENT = RevisionRef.of_git_commit("parent")
_row = st.builds(
    MetricRow,
    name=st.just("throughput"),
    value=st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False),
    direction=st.just("max"),
    unit=st.just("tok/s"),
)
_FAILURES = st.sampled_from(
    (
        None,
        Failure(kind=FailureKind.STOPPED),
        Failure(kind=FailureKind.INVALID_REPLY, role=AgentName.IMPLEMENTER),
    )
)


@st.composite
def _snapshots(draw: st.DrawFn) -> RunSnapshot:
    """A run in progress: workstreams scheduled in order, some running, some finished."""
    finished: list[FinishedWork] = []
    running: list[Running] = []
    hypotheses: list[HypothesisRecord] = []
    for sequence in range(1, draw(st.integers(0, 5)) + 1):
        plan = WorkPlan(
            kind=WorkKind.IMPLEMENT,
            work_id=f"w{sequence}",
            title=draw(st.sampled_from(("", f"Title {sequence}"))),
        )
        rounds: tuple[RoundRecord, ...] = ()
        if draw(st.booleans()):
            if draw(st.booleans()):
                finished.append(
                    FinishedWork(
                        sequence=sequence, plan=plan, parent=_PARENT, failure=draw(_FAILURES)
                    )
                )
                rounds = (
                    RoundRecord(
                        sequence=sequence,
                        attempt=AttemptId(root=f"attempt-{sequence}"),
                        review_passed=draw(st.none() | st.booleans()),
                        metrics=tuple(draw(st.lists(_row, max_size=2))),
                        failure=draw(_FAILURES),
                    ),
                )
            else:
                finished.append(FinishedWork(sequence=sequence, plan=plan, parent=_PARENT))
        else:
            running.append(Running(sequence=sequence, plan=plan))
        hypotheses.append(
            HypothesisRecord(
                hypothesis_id=plan.work_id,
                title=plan.title,
                hypothesis="claim",
                first_sequence=sequence,
                rounds=rounds,
            )
        )
    leading = (
        draw(st.none() | st.sampled_from([item.hypothesis_id for item in hypotheses]))
        if hypotheses
        else None
    )
    baseline = draw(
        st.sampled_from((NotConfigured(), Measured(reading=InputReading(accuracy_passed=True))))
    )
    return RunSnapshot(
        baseline=baseline,
        hypotheses=tuple(hypotheses),
        finished=tuple(finished),
        running=tuple(running),
        leading=leading,
    )


def _rounds(snapshot: RunSnapshot) -> list[RoundRecord]:
    return [record for item in snapshot.hypotheses for record in item.rounds]


def _workstreams(tree: IterationTree) -> list:
    return [node for node in tree.nodes if node.parent_id is None]


@settings(max_examples=examples(pr=10, nightly=60), deadline=None)
@given(_snapshots(), st.integers(0, 50))
def test_projection_copies_recorded_rounds_and_only_measured_numbers(
    snapshot: RunSnapshot, revision: int
) -> None:
    projection = project_run_snapshot(snapshot, experiment_revision=revision)
    view = agent_projection(_run_view(projection.payload))
    recorded = _rounds(snapshot)

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
        item.hypothesis_id for item in snapshot.hypotheses
    ]


@settings(max_examples=examples(pr=10, nightly=60), deadline=None)
@given(_snapshots())
def test_every_scheduled_workstream_is_one_node_in_the_order_it_was_scheduled(
    snapshot: RunSnapshot,
) -> None:
    tree = project_run_snapshot(snapshot, experiment_revision=1).iterations
    assert isinstance(tree, IterationTree)

    roots = _workstreams(tree)

    scheduled = sorted(
        [*((item.sequence, item.plan) for item in snapshot.finished)]
        + [(item.sequence, item.plan) for item in snapshot.running],
        key=lambda entry: entry[0],
    )
    assert [(node.id, node.ordinal) for node in roots] == [
        (f"workstream:{plan.work_id}", sequence) for sequence, plan in scheduled
    ]
    assert all(node.kind is IterationKind.WORKSTREAM for node in roots)
    assert [node.label for node in roots] == [plan.title or plan.work_id for _, plan in scheduled]


@settings(max_examples=examples(pr=10, nightly=60), deadline=None)
@given(_snapshots())
def test_each_recorded_round_is_an_attempt_under_its_workstream(snapshot: RunSnapshot) -> None:
    tree = project_run_snapshot(snapshot, experiment_revision=1).iterations
    assert isinstance(tree, IterationTree)

    attempts = [node for node in tree.nodes if node.parent_id is not None]

    rounds = {record.sequence: record for record in _rounds(snapshot)}
    assert sorted(node.ordinal for node in attempts) == sorted(rounds)
    for node in attempts:
        record = rounds[node.ordinal]
        assert node.id == record.attempt.root
        assert node.kind is IterationKind.ATTEMPT
        assert (node.headline_metrics == ()) is (not record.metrics)
    for root in _workstreams(tree):
        assert root.child_count == sum(node.parent_id == root.id for node in attempts)


@settings(max_examples=examples(pr=10, nightly=100), deadline=None)
@given(_snapshots())
def test_a_workstream_status_follows_how_it_ended(snapshot: RunSnapshot) -> None:
    tree = project_run_snapshot(snapshot, experiment_revision=1).iterations
    assert isinstance(tree, IterationTree)
    status = {node.id: node.status for node in tree.nodes}
    rounds = {record.sequence: record for record in _rounds(snapshot)}

    for item in snapshot.running:
        assert status[f"workstream:{item.plan.work_id}"] is IterationStatus.ACTIVE
    for item in snapshot.finished:
        record = rounds.get(item.sequence)
        failure = item.failure if record is None else record.failure or item.failure
        if failure is not None:
            expected = (
                IterationStatus.CANCELLED
                if failure.kind is FailureKind.STOPPED
                else IterationStatus.FAILED
            )
        elif record is not None and record.review_passed is False:
            expected = IterationStatus.FAILED
        else:
            expected = IterationStatus.COMPLETED
        assert status[f"workstream:{item.plan.work_id}"] is expected
        if record is not None:
            assert status[record.attempt.root] is expected


@settings(max_examples=examples(pr=10, nightly=60), deadline=None)
@given(_snapshots())
def test_only_the_leader_is_kept_and_only_running_work_is_active(snapshot: RunSnapshot) -> None:
    view = agent_projection(
        _run_view(project_run_snapshot(snapshot, experiment_revision=1).payload)
    )

    assert view is not None
    assert [item.hypothesis_id for item in view.hypotheses if item.kept] == (
        [] if snapshot.leading is None else [snapshot.leading]
    )
    running = {item.plan.work_id for item in snapshot.running}
    assert {item.hypothesis_id for item in view.hypotheses if item.active} == running
    assert view.active_hypothesis_id in (running or {None})


def _run_view(payload: dict[str, JsonValue] | None) -> RunView:
    return RunView(run_id="r", loop="dynamic", status=RunStatus.ACTIVE, projection=payload)


def _project_with_run(root: Path) -> tuple[Project, str]:
    project = Project.open(root)
    project.state.create_project("projection", now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "projection",
        branch="test",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="dynamic", config_version=1, options={}),
        trusted_input_baseline="a" * 40,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)
    return project, manifest.run_id


def _published(snapshot: RunSnapshot) -> FlowState:
    """The state of a started dynamic search whose last publication is ``snapshot``."""
    state = begin(config(), restarting=False).run.state
    return state.model_copy(
        update={"live": state.live.model_copy(update={"published": snapshot.model_dump_json()})}
    )


def _commit(project: Project, run_id: str, state: FlowState) -> RuntimeRecord[FlowState]:
    declaration = dynamic.declaration
    core = initial_state()
    core = core.model_copy(update={"run": core.run.model_copy(update={"declaration": declaration})})
    record = RuntimeRecord[FlowState].fresh(
        RunEnvelope[FlowState](
            schema_version=ENVELOPE_SCHEMA_VERSION,
            fence=HostFence(host_id=HostId(root="writer"), epoch=1),
            strategy_id=declaration.strategy_id,
            state_schema=declaration.state_schema,
            core=core,
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


def _snapshot() -> RunSnapshot:
    plan = WorkPlan(kind=WorkKind.IMPLEMENT, work_id="fuse", title="Fuse")
    return RunSnapshot(
        baseline=NotConfigured(),
        hypotheses=(
            HypothesisRecord(
                hypothesis_id="fuse",
                title="Fuse",
                hypothesis="fusing helps",
                first_sequence=1,
                rounds=(
                    RoundRecord(
                        sequence=1,
                        attempt=AttemptId(root="attempt-1"),
                        review_passed=True,
                        metrics=(
                            MetricRow(name="throughput", value=81.5, direction="max", unit="tok/s"),
                        ),
                    ),
                ),
            ),
        ),
        finished=(FinishedWork(sequence=1, plan=plan, parent=_PARENT),),
        leading="fuse",
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


def test_a_record_that_published_nothing_projects_to_an_empty_run(tmp_path: Path) -> None:
    project, run_id = _project_with_run(tmp_path)
    state = begin(config(), restarting=False).run.state
    _commit(project, run_id, state)

    view = dynamic_projector().view(project, run_id, status=RunStatus.ACTIVE, loop="dynamic")

    assert view.projection == {}
    assert view.rounds == ()


def test_the_stored_record_projects_at_its_core_revision(tmp_path: Path) -> None:
    project, run_id = _project_with_run(tmp_path)
    record = _commit(project, run_id, _published(_snapshot()))

    view = dynamic_projector().view(project, run_id, status=RunStatus.COMPLETED, loop="dynamic")

    expected = project_run_snapshot(_snapshot(), experiment_revision=record.envelope.revision)
    assert view.projection == expected.payload
    assert view.rounds == expected.rounds
    assert view.experiment_revision == record.envelope.revision
    assert view.status is RunStatus.COMPLETED


def test_a_just_committed_record_projects_without_reading_disk(tmp_path: Path) -> None:
    project, run_id = _project_with_run(tmp_path)
    state = _published(_snapshot())
    record = _commit(project, run_id, state)
    projector = dynamic_projector()

    view = projector.project_committed("dynamic", record, run_id=run_id)

    assert view == projector.view(project, run_id, status=RunStatus.ACTIVE, loop="dynamic")
    assert projector.project_committed("another-plugin", record, run_id=run_id) is None
    assert projector.project_committed("dynamic", state, run_id=run_id) is None


def test_a_plugin_registers_the_projector(tmp_path: Path) -> None:
    project, run_id = _project_with_run(tmp_path)
    _commit(project, run_id, _published(_snapshot()))
    plugin = failing_plugin("dynamic", options=_Options)

    registration = OrchestrationRegistration(plugin=plugin, projector=dynamic_projector())
    view = project_run(
        registration, project, run_id=run_id, status=RunStatus.ACTIVE, loop="dynamic"
    )

    assert agent_projection(view) is not None
    assert [item.number for item in view.rounds] == [1]
