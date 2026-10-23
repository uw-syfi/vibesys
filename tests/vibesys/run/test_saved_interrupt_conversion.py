"""Pre-schema interrupt exceptions acquire one stopped outcome during conversion."""

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vibesys.api.transport import ConversionStatus, convert_run
from vibesys.events import CoreEvent, CoreEventType, EventStatus, RunStartedData, SpanStartedData
from vs_core.api import Span, SpanStatus, SpanType
from vs_project.api import Project
from vs_proto.api import Outcome
from vs_runlog.api import read_run_head
from vs_sim.api.testing import ManualClock, SimThreads


@pytest.mark.regression
@example(detail="", interrupted=True)
@example(detail="mentions KeyboardInterrupt", interrupted=False)
@given(detail=st.text(alphabet="abc KeyboardInterrupt012:", max_size=30), interrupted=st.booleans())
def test_pre_schema_exception_conversion_preserves_the_actual_outcome(
    detail: str, *, interrupted: bool
) -> None:
    """Generate legacy exception reasons, including ordinary failures mentioning interrupts."""
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as env:
        root = Path(temporary) / "project"
        root.mkdir()
        env.setenv("VIBESYS_STATE_HOME", str(Path(temporary) / "home"))
        project = Project.open(root)
        events = [
            CoreEvent(
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                type=CoreEventType.RUN_STARTED,
                data=RunStartedData(outer_loop="dynamic", input="input"),
            ),
            CoreEvent(
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
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
            ),
            CoreEvent(
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                type=CoreEventType.RUN_FAILED,
                status=EventStatus.FAILED,
                text=f"{'KeyboardInterrupt' if interrupted else 'ValueError'}: {detail}",
            ),
        ]
        source = Project.event_journal_path_for(root, "run-a")
        source.parent.mkdir(parents=True)
        before = b"".join(
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
        source.write_bytes(before)
        result = convert_run(project, "run-a", clock=ManualClock(), threads=SimThreads())
        assert result.status is ConversionStatus.CONVERTED
        head = read_run_head(Project.run_log_path_for(root, "run-a"))
        assert head.lifecycle is not None
        assert head.lifecycle.state is not None
        assert head.lifecycle.state.field == "ended"
        assert head.lifecycle.state.value.outcome is (
            Outcome.STOPPED if interrupted else Outcome.FAILED
        )
        assert source.read_bytes() == before
