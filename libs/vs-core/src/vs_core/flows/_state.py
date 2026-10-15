"""The persisted state of a search: the journal replay needs, and what a restart needs."""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, TypeAdapter

from vs_core._registry import ContractError
from vs_core.types.common import AttemptRef, DecisionId, RunFacts, Seconds, Value
from vs_core.types.kinds import EventKind
from vs_core.types.search import (
    AdoptionFinished,
    CheckpointKnown,
    Event,
    Interrupted,
    MeasurementFinished,
    MessageDelivered,
    MessageDropped,
    OperationFinished,
    ResumeReady,
    StopRequested,
    TimeReached,
    TurnEnded,
    TurnYielded,
    WorkspaceClosed,
    WorkspaceOpened,
    YieldFailed,
)
from vs_core.types.settlement import Selection
from vs_core.types.strategy import Decision, StrategyState
from vs_core.types.until import Failed

from ._spans import RunTrace

if TYPE_CHECKING:
    from vs_core._registry import OperationRegistry


class RecordedOperation(Value):
    """An ``OperationFinished`` as the journal keeps it: the result as JSON.

    The result's outcome is a registered model, which a state schema may not name, so it is
    rebuilt through the operation registry the search was given.
    """

    kind: Literal["recorded_operation"] = "recorded_operation"
    decision: DecisionId
    result_json: str


type Recorded = RecordedOperation

type JournaledEvent = Annotated[
    WorkspaceOpened
    | WorkspaceClosed
    | MeasurementFinished
    | AdoptionFinished
    | StopRequested
    | TimeReached
    | MessageDelivered
    | MessageDropped
    | Interrupted
    | TurnEnded
    | TurnYielded
    | YieldFailed
    | ResumeReady
    | CheckpointKnown
    | Failed,
    Field(discriminator="kind"),
]
"""Every ``Event`` that is journaled as it is: all but an ``OperationFinished``."""


class Dropped(Value):
    """A result that reached no waiting flow (its wait was abandoned), and was dropped."""

    event: EventKind
    decision: DecisionId | None


class Step(Value):
    """What one step did: its input, the reads it made, and a digest of its output.

    The first step of a journal has no input: it started the flows (``init``, or a restart).
    """

    event: JournaledEvent | None = None
    recorded: Recorded | None = None
    reads: tuple[str, ...] = ()
    digest: str
    at: Seconds
    """The run-clock time of the step: the time of every span transition it recorded.

    An input like the event, so it is not in the digest; replay takes it from here.
    """


class Issued(Value):
    """How many decisions a flow has numbered under one id root (ids never repeat)."""

    path: str
    label: str
    count: int


class LiveFlow(Value):
    """A flow that is running, with the latest snapshot it can be restarted from.

    ``name`` is None for a flow that is not registered, which cannot be restarted.
    """

    path: str
    name: str | None
    snapshot_json: str
    holds: tuple[AttemptRef, ...] = ()
    """Workspaces the flow had open: a restart settles them, so none leaks."""
    checkpointed: bool = False
    """Whether the flow recorded a snapshot with ``ctx.checkpoint`` (its start is not one)."""


class LiveFlows(Value):
    """Every running flow, parent first, and the numbering that keeps decision ids unique."""

    flows: tuple[LiveFlow, ...]
    issued: tuple[Issued, ...]
    stop: StopRequested | None
    """The stop that arrived, if any: restarted flows are stopped again."""
    published: str | None
    """The flow's published record as JSON (``ctx.publish``), kept beside the journal."""
    choice: Selection | None = None
    """The winner named so far (``ctx.choose``): restarted flows keep it."""
    trace: RunTrace
    """The run's spans so far: a restart continues them."""


class Waiting(Value):
    """What one suspended flow waits for: its path (empty for the root) and its sources."""

    flow: str
    sources: tuple[str, ...]


class FlowState(StrategyState):
    """The persisted state of a flow search.

    ``journal`` replays the flows from ``base`` (None: from ``init``; otherwise from the
    snapshots of the last restart). ``live`` is where the flows are now, kept so that a code
    change can restart them from their snapshots instead of replaying.
    """

    facts: RunFacts
    journal: tuple[Step, ...]
    live: LiveFlows
    base: LiveFlows | None = None
    dropped: tuple[Dropped, ...] = ()
    """Results since the last restart that reached no waiting flow (abandoned or unknown)."""


_DECISIONS = TypeAdapter(list[Decision])


def digest(
    before: str, item: Event | Recorded | None, reads: tuple[str, ...], out: list[Decision]
) -> str:
    """A hash chained over the history, so equal digests mean equal journals so far."""
    parts = [
        before,
        item.model_dump_json() if item else "",
        *reads,
        _DECISIONS.dump_json(out).decode(),
    ]
    return sha256("\n".join(parts).encode()).hexdigest()


def recorded(
    event: Event, registry: OperationRegistry | None
) -> tuple[JournaledEvent | None, Recorded | None]:
    """The journal's form of an event: an operation result is kept as JSON."""
    if isinstance(event, OperationFinished):
        if registry is None:
            message = "a search that waits on operations needs the operation registry"
            raise ContractError(("registry",), message)
        return None, RecordedOperation(
            decision=event.decision, result_json=event.result.model_dump_json()
        )
    return event, None


def key(state: FlowState) -> tuple[int, str]:
    return len(state.journal), state.journal[-1].digest
