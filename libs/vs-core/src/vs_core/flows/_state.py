"""The persisted state of a search: the journal replay needs, and what a restart needs."""

from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import Field, TypeAdapter, field_validator

from vs_core._registry import ContractError
from vs_core.types.common import (
    AttemptRef,
    DecisionId,
    RevisionRef,
    RunFacts,
    Seconds,
    Value,
    WorkspaceMode,
)
from vs_core.types.kinds import EventKind
from vs_core.types.measurement import ComparisonSettled
from vs_core.types.search import (
    AdoptionFinished,
    CheckpointKnown,
    Event,
    Interrupted,
    MeasurementFinished,
    MessageDelivered,
    MessageDropped,
    OperationFinished,
    ResumeFailed,
    ResumeReady,
    StopLifted,
    StopRequested,
    TimeReached,
    TurnEnded,
    TurnYielded,
    WorkspaceClosed,
    WorkspaceOpened,
    YieldFailed,
)
from vs_core.types.settlement import Selection
from vs_core.types.strategy import (
    Decision,
    Measure,
    ProposeWinner,
    Settle,
    StartAttempt,
    StrategyState,
    Withdraw,
)
from vs_core.types.until import Failed

from ._spans import RunTrace, SpanLabel

if TYPE_CHECKING:
    from collections.abc import Iterable

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
    | StopLifted
    | TimeReached
    | MessageDelivered
    | MessageDropped
    | Interrupted
    | TurnEnded
    | TurnYielded
    | YieldFailed
    | ResumeReady
    | ResumeFailed
    | CheckpointKnown
    | ComparisonSettled
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


class Held(Value):
    """A workspace a flow holds open, and what a restart does with it.

    ``carried`` says the flow's latest checkpoint was recorded while it held the workspace: a
    restart that resumes the flow from that checkpoint keeps the workspace open for it
    (``ctx.workspaces.reopen``). ``salvage`` is how the workspace settles if the restart cannot
    give it back to the flow (``ctx.workspaces.salvage``); without one it settles as cancelled
    and discarded. ``base`` and ``mode`` are how it was opened, so a reopened workspace is the
    same one. ``settling`` is the settlement the flow asked for while it is in flight: a restart
    leaves the workspace to it. A hold persisted before these fields existed is read as a bare
    attempt.
    """

    attempt: AttemptRef
    base: RevisionRef | None = None
    mode: WorkspaceMode | None = None
    carried: bool = False
    salvage: Settle | None = None
    settling: Settle | None = None


class RestartSettleReason(StrEnum):
    """Why a restart settled a workspace instead of giving it back to the flow that held it."""

    OPENED_AFTER_CHECKPOINT = "opened-after-checkpoint"
    """The flow opened it after its latest checkpoint: the flow resumes before it opened it."""
    FLOW_NOT_RESUMABLE = "flow-not-resumable"
    """The flow that held it is not a registered flow with a checkpoint, so it starts afresh."""
    NOT_RESTARTED = "not-restarted"
    """The new code did not start the flow again in the step that restarted the run."""
    NOT_REOPENED = "not-reopened"
    """The flow was resumed, and ended without reopening it."""
    SETTLING = "settling"
    """Nothing was issued: the flow had asked for its settlement, which core goes on with."""
    UNCLAIMED = "unclaimed"
    """It opened after the flow that asked for it stopped waiting for it (a restart dropped the
    request, or the flow ended first): no flow holds it."""


class RestartSettled(Value):
    """A workspace a restart settled itself: the flow that held it, why, and what was kept."""

    attempt: AttemptRef
    flow: str
    reason: RestartSettleReason
    retention: Literal["discard", "wip", "candidate"]


class InFlight(Value):
    """A workspace, a measurement or a settlement a flow waited for when the run restarted.

    It waits for a resumed flow that asks for it again (see ``_run.issue``). ``path`` and
    ``label`` are the flow and the id root it issued the request under.
    """

    path: str
    label: str
    request: Measure | Withdraw | StartAttempt | ProposeWinner
    answer: (
        Annotated[
            WorkspaceOpened | WorkspaceClosed | AdoptionFinished | Failed,
            Field(discriminator="kind"),
        ]
        | None
    ) = None
    """Core's answer to an opening or a settlement, when it came before the resumed flow asked
    again: the flow takes it then."""


class LiveFlow(Value):
    """A flow that is running, with the latest snapshot it can be restarted from.

    ``name`` is None for a flow that is not registered, which cannot be restarted.
    """

    path: str
    name: str | None
    snapshot_json: str
    holds: tuple[Held, ...] = ()
    """Workspaces the flow had open: a restart gives each back or settles it, so none leaks."""

    checkpointed: bool = False
    """Whether the flow recorded a snapshot with ``ctx.checkpoint`` (its start is not one)."""

    @field_validator("holds", mode="before")
    @classmethod
    def _legacy_holds(cls, value: object) -> object:
        """Read a hold persisted as a bare attempt (before ``Held``) as a hold of that attempt."""
        if not isinstance(value, list | tuple):
            return value
        # A before-validator hands JSON over as Python lists, which a strict tuple refuses:
        # decode each hold here, leniently, from what ``Held`` itself wrote.
        return tuple(
            Held.model_validate(item if "attempt" in item else {"attempt": item}, strict=False)
            if isinstance(item, dict)
            else item
            for item in value
        )


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
    choice_label: SpanLabel | None = None
    """The label ``ctx.choose`` gave the choice: what the span that adopts it is labelled."""
    in_flight: tuple[InFlight, ...] = ()
    """The measurements the flows wait for: a restart gives each to the resumed flow that asks
    for it again, instead of measuring twice."""
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
    restart_settled: tuple[RestartSettled, ...] = ()
    """The workspaces the restarts since this journal began settled themselves, with why."""

    def extended(self, changes: Iterable[bytes]) -> Self:
        """This state with each of ``changes`` (a ``FlowChanges`` from a log) applied in order.

        ``live`` keeps the flows' snapshots of the checkpoint it came from, which is where a
        restart on changed code starts, but its decision numbers are current: the restarted flows
        never reuse the id of a decision the dead ones issued. The steps are joined once, so
        reading a long run of changes costs their size and not the journal's per change.
        """
        steps = list(self.journal)
        dropped, settled = self.dropped, self.restart_settled
        published = self.live.published
        issued = {(i.path, i.label): i for i in self.live.issued}
        for encoded in changes:
            gained = FlowChanges.model_validate_json(encoded)
            steps.extend(gained.steps)
            dropped, settled = gained.dropped, gained.restart_settled
            published = published if gained.published is None else gained.published
            issued.update({(i.path, i.label): i for i in gained.issued})
        live = self.live.model_copy(
            update={"issued": tuple(issued.values()), "published": published}
        )
        return self.model_copy(
            update={
                "journal": tuple(steps),
                "dropped": dropped,
                "restart_settled": settled,
                "live": live,
            }
        )


class FlowChanges(Value):
    """What one advance added to a ``FlowState``.

    The journal steps, and the two fields every update replaces. The log keeps this between
    checkpoints, so it grows with the steps and not with the state.
    """

    steps: tuple[Step, ...]
    dropped: tuple[Dropped, ...] = ()
    restart_settled: tuple[RestartSettled, ...] = ()
    issued: tuple[Issued, ...] = ()
    """The decision numbers the steps moved: a restart from an older checkpoint must number past
    every id the dead flows issued, or an answer for an old decision reaches a new one."""
    published: str | None = None
    """The record the flows published in these steps, when they did: what a reader of the log
    shows of the run, which the steps alone do not rebuild without running the flows."""


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
