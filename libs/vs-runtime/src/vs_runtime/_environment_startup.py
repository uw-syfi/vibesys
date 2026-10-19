"""What a run environment reports while it opens: the stage it is in, its progress, its output.

Opening a container environment builds the agent image (the first build of a
host takes minutes) and then starts the container. Each step is reported to
the request's ``startup`` sink as a typed observation, so a caller can show
where a starting run is without reading log lines. The raw output of each
startup command (``docker build``, ``docker push``, ``docker run``) goes to a
stream the request's ``startup_output`` opener provides, for a person to read.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager

    from vs_agent.api.images import ImageBuildProgress
    from vs_sim.api import Clock, OutputSink


class EnvironmentStartupStage(StrEnum):
    """Where an opening environment is, in the order it gets there."""

    PROBING_WORKSPACE = "probing_workspace"
    """A scheduler job proves the workspace root is shared with the compute nodes."""
    BUILDING_IMAGE = "building_image"
    """The agent image (and the task image under it, if any) is being built."""
    BUILDING_TOOLS = "building_tools"
    """The image is built; the evaluator tools it lacks in cache are being built."""
    STARTING_CONTAINER = "starting_container"
    """The image is built; the agent's container is being started."""


class EnvironmentStartupState(StrEnum):
    """What a stage is doing at the moment one observation is made."""

    RUNNING = "running"
    """The stage began, or moved to a new step of its own (``progress``)."""
    WAITING = "waiting"
    """The stage is blocked on something outside this process, named by ``condition``."""
    FINISHED = "finished"
    """The stage ended without failing; a failure is the environment's typed error instead."""


class EnvironmentWaitCondition(StrEnum):
    """What a waiting stage waits on."""

    QUEUED = "queued"
    """A scheduler holds the job in its queue."""
    RUNNING = "running"
    """The scheduler runs the job; the stage waits for it to end."""


@dataclass(frozen=True)
class EnvironmentStartup:
    """One startup observation: the stage, what it is doing, and how long it has taken.

    ``state`` is ``RUNNING`` for the stage's start and each step of its
    ``progress``. A ``WAITING`` observation carries the ``condition`` and the
    seconds waited so far; a ``FINISHED`` one the seconds the whole stage took.
    ``elapsed_seconds`` is on a clock of the environment's own, so a consumer
    reads durations from it and never from its own receipt times.
    """

    stage: EnvironmentStartupStage
    progress: ImageBuildProgress | None = None
    state: EnvironmentStartupState = EnvironmentStartupState.RUNNING
    condition: EnvironmentWaitCondition | None = None
    elapsed_seconds: float | None = None

    def __post_init__(self) -> None:
        """Reject a combination of fields that no observation can have."""
        waiting = self.state is EnvironmentStartupState.WAITING
        if (self.condition is not None) != waiting:
            message = "condition is present exactly when the state is waiting"
            raise ValueError(message)
        if (self.elapsed_seconds is not None) != (
            self.state is not EnvironmentStartupState.RUNNING
        ):
            message = "elapsed_seconds is present exactly when the state is not running"
            raise ValueError(message)
        if self.progress is not None and self.state is not EnvironmentStartupState.RUNNING:
            message = "progress is present only while the state is running"
            raise ValueError(message)


type EnvironmentStartupSink = Callable[[EnvironmentStartup], None]
"""Where an opening environment reports its startup observations."""


@contextmanager
def startup_stage(
    sink: EnvironmentStartupSink | None, stage: EnvironmentStartupStage, *, clock: Clock
) -> Iterator[None]:
    """Report *stage* running for the block, then finished with how long it took.

    A block that raises reports no ``FINISHED``: the failure is the caller's
    typed error, and a stage that never finished must not read as one that did.
    """
    started = clock.now()
    if sink is not None:
        sink(EnvironmentStartup(stage=stage))
    yield
    if sink is not None:
        sink(
            EnvironmentStartup(
                stage=stage,
                state=EnvironmentStartupState.FINISHED,
                elapsed_seconds=max(0.0, clock.now() - started),
            )
        )


def report_wait(
    sink: EnvironmentStartupSink | None,
    stage: EnvironmentStartupStage,
    condition: EnvironmentWaitCondition,
    *,
    elapsed_seconds: float,
) -> None:
    """Report that *stage* has waited *elapsed_seconds* on *condition*."""
    if sink is not None:
        sink(
            EnvironmentStartup(
                stage=stage,
                state=EnvironmentStartupState.WAITING,
                condition=condition,
                elapsed_seconds=max(0.0, elapsed_seconds),
            )
        )


type StartupOutputOpener = Callable[
    [EnvironmentStartupStage, str], AbstractContextManager[OutputSink]
]
"""Opens the stream one startup command's raw output is written to.

Called once per startup command with the stage it belongs to and a one-line
description of the command for display (``docker run <image>``). The
environment writes the command's stdout and stderr chunks to the entered sink
while the command runs, and exits the context when the command has finished,
failed, or been interrupted, on every path; after exit nothing more is written.
The stream carries output only: what happened is reported through the
``startup`` sink and the environment's typed errors.
"""


type ToolBuildOutputOpener = Callable[[tuple[str, ...]], AbstractContextManager[OutputSink]]
"""Opens the stream one evaluator tool build writes its raw output to.

Called once for each build an opening environment runs, with the names of the
evaluator tools it builds (a build makes every tool its cache lacks, in one
builder). The environment writes the stdout and stderr chunks of the build's
commands (the builder's start, its root setup, the tool installer) to the
entered sink while they run, and exits the context when the build has ended,
on every path; after exit nothing more is written. A cache hit builds nothing
and opens no stream. The stream carries output only: a failed build raises
the environment's typed error.
"""
