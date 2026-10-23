"""Pre-schema interrupt exceptions acquire one stopped outcome during conversion."""

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


def _write_journal(root: Path, ending: CoreEvent) -> bytes:
    source = Project.event_journal_path_for(root, "run-a")
    source.parent.mkdir(parents=True, exist_ok=True)
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
        for index, event in enumerate([RUN_STARTED, FLOW_STARTED, ending])
    )
    source.write_bytes(content)
    return content


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
        [KeyboardInterrupt, ValueError, RuntimeError, OSError, TimeoutError, SystemExit]
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
        assert _outcome(root) is (Outcome.STOPPED if error is KeyboardInterrupt else Outcome.FAILED)
