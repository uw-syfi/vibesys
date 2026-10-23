"""Pre-schema interrupt exceptions acquire one stopped outcome during conversion."""

import asyncio
import builtins
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vibesys.api._session import unwinding
from vibesys.api.transport import ConversionStatus, convert_run
from vibesys.events import (
    CoreEvent,
    CoreEventType,
    RunEndingData,
    RunStartedData,
    SpanStartedData,
    make_core_event,
)
from vs_core.api import Span, SpanStatus, SpanType
from vs_project.api import Project
from vs_proto.api import (
    API_SCHEMA_HASH,
    STORE_SCHEMA_HASH,
    EpochStarted,
    Oneof,
    Outcome,
    Record,
    Timestamp,
)
from vs_runlog.api import FileRunLogWriter, read_run_head
from vs_sim.api.testing import ManualClock, SimThreads

AT = datetime(2026, 1, 1, tzinfo=UTC)
RUN_STARTED = CoreEvent(
    timestamp=AT,
    type=CoreEventType.RUN_STARTED,
    data=RunStartedData(outer_loop="dynamic", input="input"),
)
FLOW_STARTED = CoreEvent(
    timestamp=AT,
    type=CoreEventType.SPAN_STARTED,
    data=SpanStartedData(
        span=Span(
            id="flow:run",
            parent=None,
            type=SpanType.FLOW,
            name="run",
            status=SpanStatus.RUNNING,
            started_at=0,
        )
    ),
)
LEGACY_TEXT = st.text(alphabet="abc KeyboardInterrupt012:", max_size=30)


def _write_segments(root: Path, segments: list[list[CoreEvent]]) -> bytes:
    """A journal of one run resumed: each segment opens with its start and ends as given."""
    source = Project.event_journal_path_for(root, "run-a")
    source.parent.mkdir(parents=True, exist_ok=True)
    events = [event for segment in segments for event in [RUN_STARTED, FLOW_STARTED, *segment]]
    content = b"".join(
        event.model_copy(
            update={
                "run_id": "run-a",
                "sequence": index + 1,
                "timestamp": datetime(2026, 1, 1, 0, 0, index, tzinfo=UTC),
            }
        )
        .model_dump_json()
        .encode()
        + b"\n"
        for index, event in enumerate(events)
    )
    source.write_bytes(content)
    return content


def _write_journal(root: Path, ending: CoreEvent) -> bytes:
    return _write_segments(root, [[ending]])


def _convert(project: Project, *, reconvert: bool = False) -> ConversionStatus:
    return convert_run(
        project, "run-a", clock=ManualClock(), threads=SimThreads(), reconvert=reconvert
    ).status


def _outcome(root: Path) -> Outcome:
    head = read_run_head(Project.run_log_path_for(root, "run-a"))
    assert head.lifecycle is not None
    assert head.lifecycle.state is not None
    assert head.lifecycle.state.field == "ended"
    return head.lifecycle.state.value.outcome


def _legacy_failure(text: str) -> CoreEvent:
    return make_core_event(CoreEventType.RUN_FAILED, text, timestamp=AT, status="failed")


@pytest.mark.regression
@example(detail="", interrupted=True)
@example(detail="mentions KeyboardInterrupt", interrupted=False)
@given(detail=LEGACY_TEXT, interrupted=st.booleans())
def test_pre_schema_exception_conversion_preserves_the_actual_outcome(
    detail: str, *, interrupted: bool
) -> None:
    """Generate legacy exception reasons, including ordinary failures mentioning interrupts."""
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as env:
        root = Path(temporary) / "project"
        root.mkdir()
        env.setenv("VIBESYS_STATE_HOME", str(Path(temporary) / "home"))
        project = Project.open(root)
        name = "KeyboardInterrupt" if interrupted else "ValueError"
        before = _write_journal(root, _legacy_failure(f"{name}: {detail}"))
        assert _convert(project) is ConversionStatus.CONVERTED
        assert _outcome(root) is (Outcome.STOPPED if interrupted else Outcome.FAILED)
        assert Project.event_journal_path_for(root, "run-a").read_bytes() == before


@pytest.mark.regression
@example(detail="", name="KeyboardInterrupt")
@given(detail=LEGACY_TEXT, name=st.sampled_from(["KeyboardInterrupt", "ValueError"]))
def test_reconverting_rebuilds_a_converted_run_from_its_untouched_journal(
    detail: str, name: str
) -> None:
    """A run an earlier build converted is rebuilt on request, and only on request."""
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as env:
        root = Path(temporary) / "project"
        root.mkdir()
        env.setenv("VIBESYS_STATE_HOME", str(Path(temporary) / "home"))
        project = Project.open(root)
        _write_journal(root, _legacy_failure(f"ValueError: {detail}"))
        assert _convert(project) is ConversionStatus.CONVERTED
        assert _outcome(root) is Outcome.FAILED
        # The earlier build recorded the interrupt as this failure; the journal says so.
        before = _write_journal(root, _legacy_failure(f"{name}: {detail}"))
        assert _convert(project) is ConversionStatus.EXISTS
        assert _outcome(root) is Outcome.FAILED
        assert _convert(project, reconvert=True) is ConversionStatus.CONVERTED
        assert _outcome(root) is (
            Outcome.STOPPED if name == "KeyboardInterrupt" else Outcome.FAILED
        )
        assert Project.event_journal_path_for(root, "run-a").read_bytes() == before


@given(detail=LEGACY_TEXT)
def test_reconverting_never_rebuilds_a_run_that_names_its_checkout(detail: str) -> None:
    """A live writer knows its checkout, which the journal cannot reproduce; the run is left as written."""
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as env:
        root = Path(temporary) / "project"
        root.mkdir()
        env.setenv("VIBESYS_STATE_HOME", str(Path(temporary) / "home"))
        project = Project.open(root)
        _write_journal(root, _legacy_failure(f"ValueError: {detail}"))
        assert _convert(project) is ConversionStatus.CONVERTED
        log = Project.run_log_path_for(root, "run-a")
        with FileRunLogWriter(log, clock=ManualClock(), threads=SimThreads()) as writer:
            writer.append(
                Record(
                    seq=writer.sequence + 1,
                    epoch=writer.epoch + 1,
                    at=Timestamp.from_datetime(AT),
                    clock=0.0,
                    body=Oneof(
                        "epoch_started",
                        EpochStarted(
                            run_id="run-a",
                            epoch=writer.epoch + 1,
                            tree_hash="0" * 40,
                            provenance_known=True,
                            vibesys_version="1",
                            api_schema_hash=API_SCHEMA_HASH,
                            store_schema_hash=STORE_SCHEMA_HASH,
                        ),
                    ),
                )
            )
        native = log.read_bytes()
        _convert(project, reconvert=True)
        assert log.read_bytes() == native


@given(
    error=st.sampled_from(
        [
            KeyboardInterrupt,
            asyncio.CancelledError,
            ValueError,
            RuntimeError,
            OSError,
            TimeoutError,
            SystemExit,
        ]
    ),
    text=LEGACY_TEXT,
)
def test_a_native_run_ends_stopped_exactly_when_the_user_interrupted_it(
    error: type[BaseException], text: str
) -> None:
    """The ending a live session writes for an exception reads back as the user would."""
    kind, reason, status = unwinding(error(text))
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as env:
        root = Path(temporary) / "project"
        root.mkdir()
        env.setenv("VIBESYS_STATE_HOME", str(Path(temporary) / "home"))
        project = Project.open(root)
        _write_journal(root, make_core_event(kind, reason, timestamp=AT, status=status))
        assert _convert(project) is ConversionStatus.CONVERTED
        assert _outcome(root) is (
            Outcome.STOPPED
            if error in {KeyboardInterrupt, asyncio.CancelledError}
            else Outcome.FAILED
        )


def _constructible(error: type[BaseException]) -> bool:
    try:
        error("text")
    except TypeError:
        return False
    return True


EXCEPTIONS = sorted(
    {
        error
        for error in (*vars(builtins).values(), *vars(asyncio).values())
        if isinstance(error, type) and issubclass(error, BaseException) and _constructible(error)
    },
    key=lambda error: error.__qualname__,
)


@pytest.mark.regression
@example(error=asyncio.CancelledError, text="")
@example(error=KeyboardInterrupt, text="")
@given(error=st.sampled_from(EXCEPTIONS), text=LEGACY_TEXT)
def test_a_pre_schema_run_reads_as_a_native_one_for_every_exception_class(
    error: type[BaseException], text: str
) -> None:
    """The class of the exception decides the ending, as it does natively: not a list of names."""
    kind, reason, status = unwinding(error(text))
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as env:
        root = Path(temporary) / "project"
        root.mkdir()
        env.setenv("VIBESYS_STATE_HOME", str(Path(temporary) / "home"))
        project = Project.open(root)
        _write_journal(root, make_core_event(kind, reason, timestamp=AT, status=status))
        assert _convert(project) is ConversionStatus.CONVERTED
        native = _outcome(root)
        _write_journal(root, _legacy_failure(f"{error.__name__}: {text}"))
        assert _convert(project, reconvert=True) is ConversionStatus.CONVERTED
        assert _outcome(root) is native
        assert native is (
            Outcome.STOPPED
            if issubclass(error, KeyboardInterrupt | asyncio.CancelledError)
            else Outcome.FAILED
        )


def _ending(kind: str) -> list[CoreEvent]:
    """How a segment of a resumed run ends, as the journals recorded it (often after run_ending)."""
    drained = make_core_event(
        CoreEventType.RUN_ENDING,
        timestamp=AT,
        data=RunEndingData(reason=None, resumable=True, rounds_finished=1),
    )
    return {
        "finished": [make_core_event(CoreEventType.RUN_FINISHED, timestamp=AT)],
        "failed": [drained, _legacy_failure("ValueError: boom")],
        "cancelled": [drained, _legacy_failure("CancelledError: ")],
        "interrupted": [drained, _legacy_failure("KeyboardInterrupt: ")],
        "stopped": [
            drained,
            make_core_event(
                CoreEventType.STOPPED, "Run stopped by request", timestamp=AT, status="interrupted"
            ),
        ],
    }[kind]


ENDINGS = ["finished", "failed", "cancelled", "interrupted", "stopped"]


@pytest.mark.regression
@example(segments=["cancelled", "stopped"])
@example(segments=["failed", "interrupted", "finished"])
@given(segments=st.lists(st.sampled_from(ENDINGS), min_size=1, max_size=6))
def test_a_resumed_run_reads_as_its_last_segment_ends(segments: list[str]) -> None:
    """An earlier segment's ending never decides the run's: each resume starts its own."""
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as env:
        root = Path(temporary) / "project"
        root.mkdir()
        env.setenv("VIBESYS_STATE_HOME", str(Path(temporary) / "home"))
        project = Project.open(root)
        _write_segments(root, [_ending(kind) for kind in segments])
        assert _convert(project) is ConversionStatus.CONVERTED
        resumed = _outcome(root)
        _write_segments(root, [_ending(segments[-1])])
        assert _convert(project, reconvert=True) is ConversionStatus.CONVERTED
        assert resumed is _outcome(root)


def test_a_run_resumed_after_interrupts_and_cancellations_reads_stopped_at_every_ending() -> None:
    """A real journal: one run Ctrl-C'd, cancelled and stopped over many resumes, trimmed to its lifecycle."""
    lines = (Path(__file__).parent / "fixtures" / "resumed-after-interrupts.jsonl").read_bytes()
    events = lines.splitlines(keepends=True)
    endings = [
        index + 1
        for index, line in enumerate(events)
        if b'"type":"stopped"' in line or b'"type":"run_failed"' in line
    ]
    assert endings[-1] == len(events)
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as env:
        root = Path(temporary) / "project"
        root.mkdir()
        env.setenv("VIBESYS_STATE_HOME", str(Path(temporary) / "home"))
        project = Project.open(root)
        source = Project.event_journal_path_for(root, "run-a")
        source.parent.mkdir(parents=True, exist_ok=True)
        for cut in endings:
            source.write_bytes(b"".join(events[:cut]))
            assert _convert(project, reconvert=True) in {
                ConversionStatus.CONVERTED,
                ConversionStatus.EXISTS,
            }
            assert _outcome(root) is Outcome.STOPPED
