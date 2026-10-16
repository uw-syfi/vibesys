"""The run's log store: its streams, each under a :data:`~vibesys.log_owners.LogOwner`.

Producers get the store as a ``vs_logs.api.LogScope[LogOwner]`` and only write; nothing
in the product reads a log back. :func:`open_run_logs` is the one place a run opens it:
run assembly calls it once, and passes the scope it returns to every producer.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING

from vibesys.log_owners import (
    LOG_OWNER,
    GateStage,
    LogOwner,
    RunNarration,
    StartupStep,
    ToolBuild,
)
from vibesys.run.measurement_events import gate_of
from vs_core.api import DecisionId, operation_span
from vs_logs.api import (
    CorruptLogError,
    FileLogStore,
    LogLimits,
    LogScope,
    MemoryLogStore,
    StreamOutcome,
)
from vs_project.api import Project
from vs_sim.api import DISCARD

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager
    from pathlib import Path

    from vs_logs.api import NarrationHandler
    from vs_runtime.api.core import StageOutputOpener, StageRun
    from vs_runtime.api.infrastructure import (
        EnvironmentStartupStage,
        StartupOutputOpener,
        ToolBuildOutputOpener,
    )
    from vs_sim.api import OutputSink, Threads

NARRATION_COMMAND = "vibesys"
"""The command a run's narration stream records: the run's own process."""

UNAVAILABLE_LIMITS = LogLimits(max_bytes=1 << 16, segment_bytes=1 << 14)
"""Per-stream bytes kept in memory when the run's store cannot be opened on disk."""


def open_run_log_store(
    project_root: Path,
    run_id: str,
    *,
    threads: Threads,
    warn: Callable[[str], object],
    limits: LogLimits | None = None,
) -> FileLogStore[LogOwner]:
    """Open the log store in ``run_id``'s log directory, closing streams a crash left open.

    ``warn`` hears when the store fails to write a stream; the producers never do.
    """
    directory = Project.log_streams_directory_for(project_root, run_id)
    return FileLogStore(directory, LOG_OWNER, threads=threads, warn=warn, limits=limits)


def startup_output_opener(scope: LogScope[LogOwner]) -> StartupOutputOpener:
    """The opener that records each startup command's output as a stream under its step."""

    def open_startup_output(
        stage: EnvironmentStartupStage, command: str
    ) -> AbstractContextManager[OutputSink]:
        return scope.open(StartupStep(stage=stage), command)

    return open_startup_output


def stage_output_opener(scope: LogScope[LogOwner]) -> StageOutputOpener:
    """The opener that records each gate stage's output as a stream under its gate.

    A stage is named the way its gate events name it: the job's handle is the
    execution, and the purpose and stage id give the gate; the stream's owner names the
    gate's run-trace span when the measurement's decision is known. A stage that is no gate
    (profiling) has no owner yet, so its output is discarded. The stream's command is
    the stage id, a label only.
    """

    def open_stage_output(run: StageRun) -> AbstractContextManager[OutputSink]:
        gate = gate_of(run.purpose, run.stage_id)
        if gate is None:
            return nullcontext(DISCARD)
        span = None if run.decision_id is None else operation_span(DecisionId(root=run.decision_id))
        owner = GateStage(execution_id=run.handle_id, gate=gate, attempt=run.attempt, span_id=span)
        return scope.open(owner, run.stage_id)

    return open_stage_output


TOOL_BUILD_COMMAND = "evaluator tool build"
"""The command a tool build's stream records: a label for the whole build."""

_TOOL_BUILD_NAMES_SHOWN = 4


def tool_build_name(tools: tuple[str, ...]) -> str:
    """A build's ``ToolBuild`` name: its tools sorted and joined, at most a few named.

    ``("perf", "nsys")`` is ``"nsys, perf"``; a build of six tools names the first four
    and ``"+2 more"``. A build named no tools is ``"evaluator tools"``.
    """
    names = sorted(set(tools))
    if not names:
        return "evaluator tools"
    shown = ", ".join(names[:_TOOL_BUILD_NAMES_SHOWN])
    hidden = len(names) - _TOOL_BUILD_NAMES_SHOWN
    return shown if hidden <= 0 else f"{shown} +{hidden} more"


def tool_build_output_opener(scope: LogScope[LogOwner]) -> ToolBuildOutputOpener:
    """The opener that records each evaluator tool build's output as a stream of its own."""

    def open_tool_build_output(tools: tuple[str, ...]) -> AbstractContextManager[OutputSink]:
        return scope.open(ToolBuild(name=tool_build_name(tools)), TOOL_BUILD_COMMAND)

    return open_tool_build_output


@contextmanager
def open_run_logs(
    project_root: Path,
    run_id: str,
    *,
    threads: Threads,
    warn: Callable[[str], object],
    narration: NarrationHandler | None,
) -> Iterator[LogScope[LogOwner]]:
    """Open the run's log store and, with ``narration``, its narration stream.

    Inside the block the narration stream is attached to ``narration``; leaving it stops
    the handler and ends the stream. When the store cannot be opened (its directory
    cannot be written, or its index holds a corrupt record), the run goes on: ``warn``
    says so, producers write to a bounded in-memory store nobody reads, and
    ``narration`` is closed, so records reach only what else handles them (stderr). A
    store that fails later says so through ``warn`` and never fails the run.
    """
    try:
        store = open_run_log_store(project_root, run_id, threads=threads, warn=warn)
        stream = None if narration is None else store.open(RunNarration(), NARRATION_COMMAND)
    except (OSError, CorruptLogError) as exc:
        warn(
            f"vibesys: the run's log streams are unavailable ({exc}); "
            "its narration reaches only stderr.\n"
        )
        if narration is not None:
            narration.close()
        yield MemoryLogStore(LOG_OWNER, threads=threads, warn=warn, limits=UNAVAILABLE_LIMITS)
        return
    if narration is None or stream is None:
        yield store
        return
    narration.attach(stream)
    try:
        yield store
    finally:
        narration.close()
        stream.close(StreamOutcome.ENDED)
