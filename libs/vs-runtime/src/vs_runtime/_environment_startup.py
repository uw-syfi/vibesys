"""What a run environment reports while it opens: the stage it is in, its progress, its output.

Opening a container environment builds the agent image (the first build of a
host takes minutes) and then starts the container. Each step is reported to
the request's ``startup`` sink as a typed observation, so a caller can show
where a starting run is without reading log lines. The raw output of each
startup command (``docker build``, ``docker push``, ``docker run``) goes to a
stream the request's ``startup_output`` opener provides, for a person to read.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from vs_agent.api.images import ImageBuildProgress
    from vs_sim.api import OutputSink


class EnvironmentStartupStage(StrEnum):
    """Where an opening environment is, in the order it gets there."""

    BUILDING_IMAGE = "building_image"
    """The agent image (and the task image under it, if any) is being built."""
    STARTING_CONTAINER = "starting_container"
    """The image is built; the agent's container is being started."""


@dataclass(frozen=True)
class EnvironmentStartup:
    """One startup observation: the stage, and the image build's progress within it."""

    stage: EnvironmentStartupStage
    progress: ImageBuildProgress | None = None


type EnvironmentStartupSink = Callable[[EnvironmentStartup], None]
"""Where an opening environment reports its startup observations."""


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
