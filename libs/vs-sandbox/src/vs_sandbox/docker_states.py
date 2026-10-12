"""The one Docker lifecycle translator: raw ``docker rm`` result to a typed container life.

The Docker CLI reports whether a container is gone only through the exit code
and the stderr text of ``docker rm -f``. This module is the only place that
text is interpreted; every other module reads a :data:`ContainerLife`.

The contract of :func:`observe_removal`:

* Forward only. A life orders ContainerRemoving < ContainerGone. A reading that would move it
  backwards is ignored, and ContainerGone is final, so a failed retry cannot un-remove a
  container that was removed.
* No guessing. A failure whose text is not one of the documented "already gone"
  or "already being removed" messages is ``ContainerUnknown(raw)``: the container may
  still exist, so its owner keeps it.
* Typed. Why a container is gone is a :class:`RemovedBy`; whether the owner may
  stop tracking it is :func:`released`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

_NO_SUCH_CONTAINER = "No such container"
_REMOVAL_OF = "removal of container"
_IN_PROGRESS = "is already in progress"


class RemovedBy(StrEnum):
    """How a container came to be gone."""

    REMOVED = "removed"
    ALREADY_ABSENT = "already_absent"


class _Life(BaseModel):
    """Base of the lives; strict and frozen so a compared life is exact."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ContainerRemoving(_Life):
    """The daemon is already removing the container; it will be gone without more help."""

    kind: Literal["removing"] = "removing"


class ContainerGone(_Life):
    """Final. ``outcome`` says how the container came to be gone."""

    kind: Literal["gone"] = "gone"
    outcome: RemovedBy


class ContainerUnknown(_Life):
    """The container may still exist. ``raw`` is the unrecognised failure text."""

    kind: Literal["unknown"] = "unknown"
    raw: str | None = None
    returncode: int | None = None


ContainerLife = Annotated[
    ContainerRemoving | ContainerGone | ContainerUnknown, Field(discriminator="kind")
]

INITIAL_CONTAINER_LIFE: ContainerLife = ContainerUnknown()


@dataclass(frozen=True)
class RemovalReading:
    """The exit code and stderr of one ``docker rm -f``."""

    returncode: int
    stderr: str = ""


_RANK = {"unknown": -1, "removing": 0, "gone": 1}


def removal_reading(raw: RemovalReading) -> ContainerLife:
    """What one ``docker rm -f`` result says on its own. Total."""
    if raw.returncode == 0:
        return ContainerGone(outcome=RemovedBy.REMOVED)
    if _NO_SUCH_CONTAINER in raw.stderr:
        return ContainerGone(outcome=RemovedBy.ALREADY_ABSENT)
    if _REMOVAL_OF in raw.stderr and _IN_PROGRESS in raw.stderr:
        return ContainerRemoving()
    return ContainerUnknown(raw=raw.stderr.strip(), returncode=raw.returncode)


def advance(prev: ContainerLife, seen: ContainerLife) -> ContainerLife:
    """Join a reading's life into the held life, forward only.

    An unknown reading carries no order: it only replaces another unknown.
    """
    if isinstance(seen, ContainerUnknown):
        return seen if isinstance(prev, ContainerUnknown) else prev
    if isinstance(prev, ContainerUnknown):
        return seen
    if isinstance(prev, ContainerGone):
        return prev
    return seen if _RANK[seen.kind] >= _RANK[prev.kind] else prev


def observe_removal(prev: ContainerLife, raw: RemovalReading) -> ContainerLife:
    """Fold one ``docker rm -f`` result into *prev*. Total, pure, and forward only."""
    return advance(prev, removal_reading(raw))


def released(life: ContainerLife) -> bool:
    """Whether the owner may stop tracking the container: it is gone or going."""
    return isinstance(life, (ContainerRemoving, ContainerGone))
