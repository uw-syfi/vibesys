"""Where a host can die in a run, and how many of those places a suite tries.

A durable boundary is one committed envelope. A host dies just before a commit reaches the
store or just after it. How many boundaries a run offers follows the session's one search
depth (`vs_sim.api.testing.depth`, set by the vs-sim plugin's `--depth`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from vs_sim.api.testing import Depth, depth, evenly_spaced

SAMPLE_SIZE = 12
"""Boundaries tried per crash kind when a suite samples."""


class CrashKind(StrEnum):
    """Where around a commit the host dies."""

    BEFORE_COMMIT = "before"
    AFTER_COMMIT = "after"


@dataclass(frozen=True, repr=False)
class CrashPoint:
    """The host dies just before or just after the commit with this 0-based index."""

    kind: CrashKind
    commit: int

    def __str__(self) -> str:
        """Read as a sentence in an assertion message: ``before commit 17``."""
        return f"{self.kind.value} commit {self.commit}"

    __repr__ = __str__


@dataclass(frozen=True)
class CrashPointPolicy:
    """How many boundaries of each kind a suite tries; ``None`` means every one."""

    per_kind: int | None

    def indexes(self, count: int) -> list[int]:
        """The positions this policy tries among ``count``.

        A sample is evenly spaced and always includes the first and the last; or every one.
        """
        return list(range(count)) if self.per_kind is None else evenly_spaced(count, self.per_kind)

    def choose(self, commits: int) -> tuple[CrashPoint, ...]:
        """The crash points of a run of ``commits`` commits: this policy's boundaries of each kind."""
        indexes = self.indexes(commits)
        return tuple(CrashPoint(kind, index) for kind in CrashKind for index in indexes)


SAMPLED = CrashPointPolicy(SAMPLE_SIZE)
EVERY_POINT = CrashPointPolicy(None)


def crash_point_policy() -> CrashPointPolicy:
    """The policy every run's `crash_points()` follows: the session's search depth."""
    return EVERY_POINT if depth() is Depth.NIGHTLY else SAMPLED
