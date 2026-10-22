"""A real stopped-run conversion whose log exceeds the Skeleton unary budget."""

from __future__ import annotations

import base64
import gzip
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from server.chat.manager import ChatManager
from server.controller import RunController
from server.execution import ExecutionTracker
from server.integration import RunIntegrationAdapter
from server.journal import WireJournal
from vibesys.api.transport import convert_run
from vibesys.events import CoreEvent, CoreEventType, RunStartedData, SpanStartedData
from vibesys.run.runlog_tee import RunLogTee
from vs_core.api import Span, SpanStatus, SpanType
from vs_project.api import Project
from vs_proto.api import Keyframe, RunHead
from vs_runlog.api import FileRunContent, FileRunLogWriter, RunLogReader
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
    with (directory / "run.log").open("rb") as stream:
        records = list(RunLogReader(stream))
    record_json = json.dumps(
        [base64.b64encode(record.to_binary()).decode("ascii") for record in records]
    ).encode()
    return {
        "records.base64.json.gz": gzip.compress(record_json, mtime=0),
        "run.log.gz": gzip.compress((directory / "run.log").read_bytes(), mtime=0),
        "run.head": head.to_binary(),
        f"keyframes/{head.committed_seq}.pb": expected,
        "expected.pb": expected,
        "legacy.events.json.gz": legacy_events(
            [CoreEvent.model_validate_json(line) for line in journal.splitlines()]
        ),
        **usage_fixtures(),
    }


def legacy_events(events: list[CoreEvent]) -> bytes:
    """Project the same journal through the shipping legacy frontend owner."""
    threads, clock = SimThreads(), ManualClock()
    condition = threads.condition(threads.rlock())
    journal = WireJournal(condition, clock=clock, threads=threads)
    executions = ExecutionTracker(condition, journal, clock=clock, threads=threads)
    controller = RunController(condition, journal, executions)
    chat = ChatManager(condition, journal, run_status=controller.run_status, threads=threads)
    integration = RunIntegrationAdapter(
        controller, executions, journal, chat, clock=clock, threads=threads
    )
    with TemporaryDirectory() as temporary:
        integration.attach(Path(temporary), run_id="run-1")
        controller.ready()
        for event in events:
            integration.project_event(event)
        encoded = (
            "[" + ",".join(event.model_dump_json() for event in journal.read()) + "]"
        ).encode()
        integration.close()
    return gzip.compress(encoded, mtime=0)


def usage_fixtures() -> dict[str, bytes]:
    """Replay retained real turn accounting through the shipping native writer.

    This pretrace capture is a producer fixture, not a claim that saved-run
    conversion can import pretrace journals. The importer explicitly refuses them.
    """
    source = (
        Path(__file__).parents[3]
        / "tests/conformance/fixtures/usage-records/codex-ten-rounds-trimmed.json"
    )
    events = [
        CoreEvent.model_validate(value) for value in json.loads(source.read_bytes())["events"]
    ]
    run_id = events[0].run_id
    if run_id is None:
        message = "retained usage fixture requires a run id"
        raise ValueError(message)
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        writer = FileRunLogWriter(root / "run.log", clock=ManualClock(), threads=SimThreads())
        content = FileRunContent(root / "blobs", root / "streams")
        with RunLogTee(writer, content, run_id) as tee:
            for event in events:
                tee.append(event.model_dump_json().encode())
            writer.checkpoint()
        head = RunHead.from_binary((root / "run.head").read_bytes())
        head.clear_field("process")
        expected = (root / "keyframes" / f"{head.committed_seq}.pb").read_bytes()
        with (root / "run.log").open("rb") as stream:
            records = list(RunLogReader(stream))
        return {
            "usage/run.log.gz": gzip.compress((root / "run.log").read_bytes(), mtime=0),
            "usage/run.head": head.to_binary(),
            "usage/expected.pb": expected,
            "usage/records.base64.json": (
                json.dumps(
                    [base64.b64encode(record.to_binary()).decode("ascii") for record in records],
                    indent=2,
                )
                + "\n"
            ).encode(),
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
