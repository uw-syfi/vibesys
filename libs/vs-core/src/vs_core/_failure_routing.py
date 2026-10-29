"""Who hears that a request ended without a result: one exhaustive table over request kinds.

A request core sends either is the work a decision asked for, or is a step core takes on
work it already owns (a poll of a job, an inspection, a cancel, a block). A step carries the
decision of the work it serves, but it is not that work: the area that sent it takes it
again when it ends at its own short bound, and the decision hears only of its own work. A
status query that outlasts its bound on a busy scheduler therefore never ends a measurement.

The table is a ``match`` over every request kind closed by ``assert_never``, so a new kind
cannot be added without saying who hears its failure, and a step can never fail the
decision it serves.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, assert_never

from .types.attempts import (
    CloseAttemptScope,
    DiscardWorkspace,
    EnsureWorkspace,
    RestoreRevision,
    RetainRevision,
    SnapshotAndRetain,
)
from .types.common import ExecuteRegisteredOperation
from .types.evaluation import (
    CancelOwnedJob,
    CollectEvidence,
    InspectOwnedJob,
    ObserveOwnedJob,
    SubmitMeasurement,
)
from .types.records import BlockRequest, CancelOwnedResource, InspectRequest
from .types.sessions import (
    CancelTurn,
    CloseSession,
    DispatchTurn,
    EnsureSession,
    InspectTurn,
    ResumeSessionTurn,
    SnapshotAndRetainRun,
)
from .types.settlement import AdoptRevision, VerifyAdoption

if TYPE_CHECKING:
    from .types.records import Request


class FailureHearer(StrEnum):
    """The party that hears a request's failure. Closed."""

    DECISION = "decision"
    """The decision that asked for the work: the search hears ``Failed``."""
    OWNING_AREA = "owning_area"
    """The core area that sent the request as a step on its own work; it takes the step
    again or ends the work itself, and the decision hears nothing of the step."""


def failure_hearer(request: Request) -> FailureHearer:
    """Who hears that ``request`` ended without a result."""
    match request:
        case (
            EnsureWorkspace()
            | RestoreRevision()
            | SnapshotAndRetain()
            | RetainRevision()
            | DiscardWorkspace()
            | CloseAttemptScope()
            | EnsureSession()
            | DispatchTurn()
            | CloseSession()
            | ResumeSessionTurn()
            | SnapshotAndRetainRun()
            | SubmitMeasurement()
            | AdoptRevision()
            | VerifyAdoption()
            | ExecuteRegisteredOperation()
        ):
            return FailureHearer.DECISION
        case (
            ObserveOwnedJob()
            | InspectOwnedJob()
            | CancelOwnedJob()
            | CollectEvidence()
            | InspectTurn()
            | CancelTurn()
            | InspectRequest()
            | CancelOwnedResource()
            | BlockRequest()
        ):
            return FailureHearer.OWNING_AREA
        case _:
            assert_never(request)
