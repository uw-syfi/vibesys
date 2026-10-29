"""Epoch declarations and lifecycle facts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from protobuf.wkt.google.protobuf.descriptor_pb import FileDescriptorSet
from protobuf.wkt.google.protobuf.wrappers_pb import StringValue

import vibesys.events as e
import vs_proto.api as p
from vibesys.events import CoreEventType, make_core_event
from vibesys.records._common import Projection, enum_value, measured_input
from vibesys.records._spans import ATTRS_TYPES

if TYPE_CHECKING:
    from datetime import datetime

    from protobuf import DescFile

    from vibesys.records._types import MappedEvent, MappingState


def descriptors() -> FileDescriptorSet:
    """Include transitive descriptors so every Any is independently decodable."""
    files: dict[str, DescFile] = {}
    pending = [
        cls.desc().file
        for cls in [
            *ATTRS_TYPES.values(),
            p.SearchRunDescription,
            p.SearchEnded,
            p.SearchRunFailure,
            p.TranscriptEntry,
            StringValue,
        ]
    ]
    while pending:
        file = pending.pop()
        if file.name not in files:
            files[file.name] = file
            pending.extend(file.dependencies)
    return FileDescriptorSet(file=[files[name].proto for name in sorted(files)])


def header(projection: Projection, run: p.RunDescription | None = None) -> None:
    if run is not None and projection.input_measurement is not None:
        # A later epoch of the same run starts from the input the earlier one measured.
        run.input_measurement = projection.input_measurement
        projection.input_inherited = measured_input(projection.input_measurement) is not None
    projection.emit(
        "epoch_started",
        p.EpochStarted(
            run_id=projection.state.run_id,
            epoch=projection.state.epoch,
            tree_hash=projection.state.tree_hash,
            dirty=projection.state.dirty,
            provenance_known=bool(projection.state.tree_hash),
            project_root=projection.state.project_root or None,
            vibesys_version=projection.state.vibesys_version,
            api_schema_hash=p.API_SCHEMA_HASH,
            store_schema_hash=p.STORE_SCHEMA_HASH,
            run=run,
            descriptors=descriptors(),
            span_kinds=[
                p.SpanKindDecl(
                    kind=kind, attrs_type=cls.desc().type_name, timeline=kind != "baseline"
                )
                for kind, cls in ATTRS_TYPES.items()
            ],
            series=[]
            if run is None
            else [
                p.MetricSeriesDecl(
                    name=o.series, direction=o.direction, role=p.SeriesRole.OBJECTIVE
                )
                for o in run.objectives
            ],
        ),
    )


def epoch_header(state: MappingState, at: datetime) -> MappedEvent:
    """The epoch-open declaration ``state`` names, before a run description exists.

    ``at`` supplies the declaration's clock. The carrier event is not stored;
    a later ``run_started`` amends this same epoch with the run description.
    """
    projection = Projection(state, make_core_event(CoreEventType.RUN_CONFIGURED, timestamp=at))
    header(projection)
    return projection.result()


def started(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, e.RunStartedData):
        message = "run_started requires RunStartedData"
        raise TypeError(message)
    # A journal that holds several segments of one run (each resume appends one) replays them in
    # one state: a segment's ending belongs to that segment and must not decide the next one's.
    projection.epoch_ended = False
    projection.ending = None
    overview = data.overview
    objective = None if overview is None else overview.objective
    evaluator = None if overview is None else overview.benchmarks.evaluator
    projection.primary_series = None if objective is None else objective.metric
    objectives = (
        []
        if objective is None
        else [
            p.Objective(series=o.name, direction=enum_value(p.Direction, o.direction))
            for o in objective.objectives
        ]
    )
    if objective is not None and not objectives and objective.metric and objective.direction:
        objectives = [
            p.Objective(
                series=objective.metric, direction=enum_value(p.Direction, objective.direction)
            )
        ]
    header(
        projection,
        p.RunDescription(
            strategy=data.outer_loop,
            input=data.input,
            objective=projection.blob(
                ("" if objective is None else objective.text).encode(), "text/markdown"
            ),
            objectives=objectives,
            correctness_check=[] if objective is None else list(objective.correctness_check),
            expected_roles=list(data.expected_roles),
            evaluator=None
            if evaluator is None
            else p.Evaluator(name=evaluator.name, version=evaluator.version),
            detail=projection.pack(p.SearchRunDescription(max_workstreams=data.max_rounds)),
        ),
    )
    projection.emit("lifecycle", p.Lifecycle(state=p.Oneof("running", p.Running())))


def starting(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, e.RunStartupProgressData):
        message = "run_startup_progress requires RunStartupProgressData"
        raise TypeError(message)
    progress = data.progress
    projection.emit(
        "lifecycle",
        p.Lifecycle(
            state=p.Oneof(
                "starting",
                p.Starting(
                    stage=enum_value(p.StartupStage, data.stage),
                    state=enum_value(p.StageState, data.state),
                    condition=None
                    if data.condition is None
                    else enum_value(p.WaitCondition, data.condition),
                    elapsed_seconds=data.elapsed_seconds,
                    progress=None
                    if progress is None
                    else p.Progress(
                        done=progress.done,
                        total=progress.total,
                        label=projection.text("run", "startup.label", progress.label),
                    ),
                ),
            )
        ),
    )

    if data.state is e.EnvironmentStartupState.FINISHED:
        projection.emit("lifecycle", p.Lifecycle(state=p.Oneof("running", p.Running())))


def ended(projection: Projection) -> None:
    event = projection.event
    ending = p.Ended() if projection.ending is None else p.Ended.from_binary(projection.ending)
    data = event.data
    if isinstance(data, e.RunEndingData):
        ending.summary = projection.text("run", "summary", data.summary)
        ending.resumable = data.resumable
        ending.detail = projection.pack(
            p.SearchEnded(
                reason=None if data.reason is None else enum_value(p.EndReason, data.reason),
                uncompared=data.uncompared,
            )
        )
        if ending.outcome != p.Outcome.FAILED:
            ending.outcome = (
                p.Outcome.STOPPED
                if data.reason is not None and data.reason.value == "stopped"
                else p.Outcome.FINISHED
            )
        if projection.epoch_ended:
            projection.emit("lifecycle", p.Lifecycle(state=p.Oneof("ended", ending)))
        else:
            projection.ending = ending.to_binary()
        return
    if isinstance(data, e.RunFailedData):
        failure = data.failure
        kind = (
            "environment_unavailable"
            if failure.kind.value == "run_environment_unavailable"
            else failure.kind.value
        )
        ending.outcome = p.Outcome.FAILED
        ending.failure = p.RunFailure(
            kind=enum_value(p.RunFailureKind, kind),
            reason=projection.text("run", "failure.reason", failure.reason),
            detail=projection.pack(
                p.SearchRunFailure(
                    workstreams_started=failure.workstreams_started,
                    workstream_budget=failure.workstream_budget,
                    candidates_kept=failure.candidates_kept,
                    adopted_candidate=failure.adopted_candidate,
                )
            ),
        )
    elif event.type is e.CoreEventType.RUN_FAILED:
        ending.outcome = p.Outcome.FAILED
        ending.failure = p.RunFailure(
            kind=p.RunFailureKind.NO_RESULT,
            reason=projection.text("run", "failure.reason", event.text or ""),
        )
    elif event.type is e.CoreEventType.STOPPED:
        ending.outcome = p.Outcome.STOPPED
    elif ending.outcome == p.Outcome.UNSPECIFIED:
        ending.outcome = p.Outcome.FINISHED
    for span in list(projection.spans.values()):
        if span.status in {p.SpanStatus.RUNNING, p.SpanStatus.WAITING, p.SpanStatus.PENDING}:
            span.status = (
                p.SpanStatus.FAILED if ending.outcome == p.Outcome.FAILED else p.SpanStatus.STOPPED
            )
            span.ended_at = max(span.started_at, projection.clock)
            span.waiting_on = []
            projection.span(span, p.Change.ENDED)
    projection.emit("lifecycle", p.Lifecycle(state=p.Oneof("ended", ending)))


def warning(projection: Projection) -> None:
    data = projection.event.data
    if data is None:
        # Older producers persist the warning text without a structured payload.
        text = projection.event.text or ""
    elif isinstance(data, e.FrameworkWarningData):
        text = data.summary + ("\n" + data.detail if data.detail else "")
    else:
        message = "framework_warning requires text or FrameworkWarningData"
        raise TypeError(message)
    projection.emit(
        "notice",
        p.Notice(
            severity=p.Severity.WARNING,
            code=p.NoticeCode.FRAMEWORK_WARNING,
            message=projection.text("run", "notice", text or "Framework warning"),
            # Notice requires a display message; retain an empty source exactly.
            detail=projection.pack(StringValue(value=text)) if not text else None,
        ),
    )


MAPPERS = {
    e.CoreEventType.RUN_STARTED: started,
    e.CoreEventType.RUN_STARTUP_PROGRESS: starting,
    e.CoreEventType.RUN_FINISHED: ended,
    e.CoreEventType.RUN_FAILED: ended,
    e.CoreEventType.RUN_ENDING: ended,
    e.CoreEventType.STOPPED: ended,
    e.CoreEventType.FRAMEWORK_WARNING: warning,
}
