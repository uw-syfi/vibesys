"""What a run environment reports while it opens: the stage it is in, and its progress.

Opening a container environment builds the agent image (the first build of a
host takes minutes) and then starts the container. Each step is reported to
the request's ``startup`` sink as a typed observation, so a caller can show
where a starting run is without reading log lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_agent.api.images import ImageBuildProgress


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
