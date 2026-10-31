"""Why the fold will not take a record, and how a caller that can race is told by value.

A log holds only records the fold took, so a refusal is the answer to "may this record be
appended now". A caller that appends live (the step loop's log) asks for the refusals a race
explains as a value: a step that got its result, or a run that paused or ran out the step's time,
between the decision and the stamp. The rest are a record that no race explains, and the fold raises
them as :class:`RecordRefusedError`, which is still a ``ValueError`` for code that reads a log.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types.common import RequestId


class RefusalReason(StrEnum):
    """The closed set of reasons the fold refuses a record."""

    NO_SEED = "no_seed"
    """A core record, or a fold, with no seed state before it."""
    SEED_REPEATED = "seed_repeated"
    """A second start of a run the log already started."""
    STEP_NOT_OPEN = "step_not_open"
    """The record names a step that has its result, is ambiguous, or the run never held."""
    START_NOT_DUE = "start_not_due"
    """A start the rule would not ``Run`` at the instant it is stamped (a pause, a park, a limit)."""
    ATTEMPT_MISMATCH = "attempt_mismatch"
    """The record names an attempt other than the one the step has in that position."""
    RUN_NOT_OVER = "run_not_over"
    """The end of a run that is not over, or whose result is not the one the run holds."""
    RESULT_MISMATCH = "result_mismatch"
    """A result or fact that does not end the step it names, or belongs to another request."""
    UNREADABLE = "unreadable"
    """A value, or an encoding version, this version of the code does not know."""


@dataclass(frozen=True)
class RecordRefusal:
    """A record the fold would not take, and why."""

    reason: RefusalReason
    tag: RequestId | None
    """The step the record names, if it names one."""
    detail: str


class RecordRefusedError(ValueError):
    """The fold refused a record: raised for a log other code wrote, returned for a live race."""

    def __init__(self, reason: RefusalReason, detail: str, tag: RequestId | None = None) -> None:
        """Say which record was refused and why."""
        super().__init__(detail)
        self.refusal = RecordRefusal(reason, tag, detail)
