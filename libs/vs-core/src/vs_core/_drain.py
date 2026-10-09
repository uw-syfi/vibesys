"""What a request is for when a run drains: work core cannot end, or cleanup core issues.

A stop moves the run to ``closing`` and core ends what it can: it cancels turns and jobs,
inspects and closes sessions, closes attempt scopes and discards workspaces. Each of those
requests is core's own drain, so the shell may wait for it. Any other running request starts
external work (a turn, a submission, a poll, a snapshot, an adoption): core has no
cancellation for it, so a stop that finds one running cannot finish through core alone.

The classification is a closed match over the request union, so a new request kind fails the
type check until someone says which side of the drain it is on.
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
from .types.intents import BlockIntent, CancelOwnedResource, InspectRequest
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
    from .types.intents import Request


class DrainRole(StrEnum):
    """Which side of a drain a request is on."""

    CLEANUP = "cleanup"
    WORK = "work"


def drain_role(request: Request) -> DrainRole:
    """Whether core issues ``request`` to end work (cleanup) or it starts external work."""
    match request:
        case (
            CancelTurn()
            | InspectTurn()
            | CloseSession()
            | CancelOwnedJob()
            | CancelOwnedResource()
            | InspectOwnedJob()
            | InspectRequest()
            | CollectEvidence()
            | CloseAttemptScope()
            | DiscardWorkspace()
            | BlockIntent()
        ):
            return DrainRole.CLEANUP
        case (
            EnsureWorkspace()
            | RestoreRevision()
            | SnapshotAndRetain()
            | SnapshotAndRetainRun()
            | RetainRevision()
            | EnsureSession()
            | DispatchTurn()
            | ResumeSessionTurn()
            | SubmitMeasurement()
            | ObserveOwnedJob()
            | AdoptRevision()
            | VerifyAdoption()
            | ExecuteRegisteredOperation()
        ):
            return DrainRole.WORK
        case _:
            assert_never(request)
