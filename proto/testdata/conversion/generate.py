"""A real stopped-run conversion whose log exceeds the Skeleton unary budget."""

from __future__ import annotations

import gzip
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from vibesys.api.transport import convert_run
from vibesys.events import CoreEvent, CoreEventType, RunStartedData, SpanStartedData
from vs_core.api import Span, SpanStatus, SpanType
from vs_project.api import Project
from vs_proto.api import Keyframe, RunHead
from vs_sim.api.testing import ManualClock, SimThreads


def fixtures(project: Project) -> dict[str, bytes]:
    """Return gateway fixtures generated through the shipping conversion entrypoint."""
    run_id = "run-1"
    directory = project.log_directory_for(project.root, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    bodies = [
        (CoreEventType.RUN_STARTED, RunStartedData(outer_loop="single-agent", input="input"), None),
        (
            CoreEventType.SPAN_STARTED,
            SpanStartedData(
                span=Span(
                    id="flow:root",
                    name="root",
                    type=SpanType.FLOW,
                    status=SpanStatus.RUNNING,
                    parent=None,
                    started_at=0.0,
                )
            ),
            None,
        ),
        *(
            (CoreEventType.FRAMEWORK_WARNING, None, "historical warning " + "x" * 32768)
            for _ in range(40)
        ),
        (CoreEventType.RUN_FINISHED, None, None),
    ]
    journal = b"".join(
        CoreEvent(
            type=kind,
            data=data,
            text=text or "",
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            run_id=run_id,
            sequence=i,
        )
        .model_dump_json()
        .encode()
        + b"\n"
        for i, (kind, data, text) in enumerate(bodies, 1)
    )
    project.event_journal_path_for(project.root, run_id).write_bytes(journal)
    convert_run(project, run_id, clock=ManualClock(), threads=SimThreads())
    head = RunHead.from_binary((directory / "run.head").read_bytes())
    head.clear_field("process")
    expected = (directory / "keyframes" / f"{head.committed_seq}.pb").read_bytes()
    frame = Keyframe.from_binary(expected)
    if frame.log_offset <= 1 << 20:
        message = "converted fixture must exceed the Skeleton replay budget"
        raise ValueError(message)
    return {
        "run.log.gz": gzip.compress((directory / "run.log").read_bytes(), mtime=0),
        "run.head": head.to_binary(),
        f"keyframes/{head.committed_seq}.pb": expected,
        "expected.pb": expected,
    }


if __name__ == "__main__":
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        os.environ["VIBESYS_STATE_HOME"] = str(root / "home")
        (root / "project").mkdir()
        for name, payload in fixtures(Project.open(root / "project")).items():
            path = Path(__file__).parent / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
