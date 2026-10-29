"""Run cards project the same committed store facts the run page opens."""

import io
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from server.host_api import (
    RUN_EVENTS_FILE,
    RunClosing,
    RunDigest,
    RunDigestUnreadable,
    RunEndOutcome,
)
from server.host_helper_os import FileProjects
from vs_project.api import Project
from vs_project.api.testing import FakeGitRepositories
from vs_proto.api import (
    Direction,
    Ended,
    EpochStarted,
    Lifecycle,
    Oneof,
    Outcome,
    Record,
    RunFailure,
    RunFailureKind,
    Running,
)
from vs_runlog.api import FileRunLogWriter, RunLogReader, read_run_head
from vs_sim.api.testing import ManualClock, SimThreads

CORPUS = Path(__file__).resolve().parents[2] / "proto/testdata/retained/64c1a8b7/run.log"


@pytest.mark.regression
@example(input_value=100, candidate=517, outcome=Outcome.STOPPED, minimize=False)
@given(
    input_value=st.integers(0, 1000),
    candidate=st.integers(0, 10000),
    minimize=st.booleans(),
    outcome=st.sampled_from(
        [Outcome.FINISHED, Outcome.FAILED, Outcome.STOPPED, Outcome.INTERRUPTED]
    ),
)
def test_native_cards_ignore_a_contradictory_legacy_journal_and_cached_digest(
    input_value: int, candidate: int, outcome: Outcome, *, minimize: bool
) -> None:
    """Generate measured retained-writer runs, every ended outcome, and missing or wrong old journals."""
    records = list(RunLogReader(io.BytesIO(CORPUS.read_bytes())))
    for record in records:
        body = record.body
        if body is not None and isinstance(body.value, EpochStarted):
            declaration = body.value
            assert declaration.run is not None
            for objective in declaration.run.objectives:
                objective.direction = Direction.MIN if minimize else Direction.MAX
            for series in declaration.series:
                series.direction = Direction.MIN if minimize else Direction.MAX
        if body is not None and body.field == "metric":
            body.value.value = input_value if body.value.span == "input-gate" else candidate
        if (
            body is not None
            and body.field == "lifecycle"
            and body.value.state
            and body.value.state.field == "ended"
        ):
            body.value.state = Oneof(
                "ended",
                Ended(
                    outcome=outcome,
                    failure=RunFailure(kind=RunFailureKind.NO_RESULT, reason="failure")
                    if outcome is Outcome.FAILED
                    else None,
                    resumable=True,
                    signal=2 if outcome is Outcome.INTERRUPTED else None,
                    interruption_reason="signal received"
                    if outcome is Outcome.INTERRUPTED
                    else None,
                ),
            )
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = Project.run_log_path_for(root, "run-a")
        path.parent.mkdir(parents=True)
        with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()) as writer:
            for record in records:
                writer.append(record)
        projects = FileProjects(FakeGitRepositories().repository, SimThreads())
        # A native run stands without a legacy journal. A later contradictory file cannot change it.
        first = projects.run_digest(root, "run-a")
        (path.parent / RUN_EVENTS_FILE).write_text(
            '{"type":"run_failed","sequence":1,"text":"wrong old outcome"}\n'
        )
        second = projects.run_digest(root, "run-a")
        assert first == second
        assert isinstance(second, RunDigest)
        head = read_run_head(path)
        assert head.objective is not None
        assert second.dashboard is not None
        assert second.dashboard.best is not None
        assert records[-1].at is not None
        assert second.dashboard.last_event_at == records[-1].at.to_datetime()
        assert (
            second.dashboard.best.value
            == head.objective.best_value
            == (min(input_value, candidate) if minimize else max(input_value, candidate))
        )
        best = head.objective.best_value
        expected = (
            None
            if input_value == 0 or (minimize and best == 0)
            else input_value / best
            if minimize and best != 0
            else best / input_value
        )
        assert second.adopted_ratio == expected
        assert second.ending is not None
        assert second.closing is (
            RunClosing.FINISHED
            if outcome is Outcome.FINISHED
            else RunClosing.FAILED
            if outcome is Outcome.FAILED
            else RunClosing.INTERRUPTED
        )
        if outcome in (Outcome.STOPPED, Outcome.INTERRUPTED):
            assert second.ending.outcome is RunEndOutcome.STOPPED
        # A later published epoch replaces both terminal facts even while the old file is unchanged.
        with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()) as writer:
            writer.append(
                Record(
                    seq=writer.sequence + 1,
                    at=records[-1].at,
                    epoch=1,
                    body=Oneof(
                        "lifecycle",
                        Lifecycle(
                            state=Oneof(
                                "running",
                                Running(),
                            )
                        ),
                    ),
                )
            )
        latest = projects.run_digest(root, "run-a")
        assert isinstance(latest, RunDigest)
        assert latest.ending is None
        assert latest.closing is None


@pytest.mark.regression
@example(head_bytes=None)
@given(head_bytes=st.one_of(st.none(), st.binary(max_size=50)))
def test_a_store_without_a_readable_head_never_uses_the_legacy_journal(
    head_bytes: bytes | None,
) -> None:
    """Every missing/torn/corrupt head of a converted or native log is a typed refusal."""
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = Project.run_log_path_for(root, "run-a")
        path.parent.mkdir(parents=True)
        path.write_bytes(CORPUS.read_bytes())
        if head_bytes is not None:
            path.with_name("run.head").write_bytes(head_bytes)
        (path.parent / RUN_EVENTS_FILE).write_text('{"type":"run_finished","sequence":1}\n')
        projects = FileProjects(FakeGitRepositories().repository, SimThreads())
        digest = projects.run_digest(root, "run-a")
        assert isinstance(digest, RunDigestUnreadable)
        assert "committed store" in digest.message
