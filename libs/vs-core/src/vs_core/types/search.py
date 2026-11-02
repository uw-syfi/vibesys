"""The events core reports to a run's flows, and the narrow read-only view of the run.

Events are past tense and typed. A result carries the decision it answers, so the flow runtime
can tell which of its waits ended. A wait that ends without a result arrives as ``Failed``
with a closed reason; nothing here is free text. A flow reads facts other owners keep (the
clock, the baseline, measurements, settlements) through ``RunView``, by way of ``ctx``, and
never copies them into its own state.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SerializeAsAny

from .agents import Checkpoint
from .attempts import AttemptBudget, AttemptPhase, AttemptView, WorkspacePlan
from .common import (
    AttemptRef,
    Capabilities,
    ControlInput,
    DecisionId,
    InvocationRef,
    ItemId,
    Limits,
    RequestId,
    RevisionRef,
    RunFacts,
    Scope,
    Seconds,
    Value,
    unset,
)
from .evaluation import (
    Continuation,
    EvidenceRef,
    MeasurementResult,
    ResumeAuthorized,
    ResumeRefused,
)
from .evaluation_history import AttemptEvaluationHistory, AttemptTerminalReason
from .kinds import EventKind
from .measurement import Comparison, ComparisonSettled, StatedComparison
from .records import OperationResult
from .session_inputs import InputDelivered, InputDropped
from .sessions import InterruptCompleted, InterruptFailed, TurnResult, YieldPublicationFailed
from .settlement import AdoptionFailed, AdoptionResult, Settlement
from .until import Failed


class WorkspaceOpened(Value):
    """An attempt's workspace is ready to use."""

    kind: Literal["workspace_opened"] = "workspace_opened"
    decision: DecisionId
    attempt: AttemptRef


class WorkspaceClosed(Value):
    """An attempt's workspace is closed and settled."""

    kind: Literal["workspace_closed"] = "workspace_closed"
    decision: DecisionId | None
    settlement: Settlement


class TurnFinished(Value):
    """What ``session.turn`` returns when the agent answered: its reply, valid against its model.

    ``decision`` is the turn request it answers. ``checkpoint`` is core's outcome for the
    revision the turn produced, present exactly when the agent's reply asks for one (the
    pipeline holds the reply until the outcome is known). A turn that ended any other way
    arrives as ``Failed``.
    """

    kind: Literal["turn_finished"] = "turn_finished"
    decision: DecisionId
    invocation: InvocationRef
    reply: SerializeAsAny[BaseModel]
    checkpoint: Checkpoint | None = None


class TurnEnded(Value):
    """Plumbing of the agent pipeline: one provider turn ended. A search never sees it."""

    kind: Literal["turn_ended"] = "turn_ended"
    decision: DecisionId
    result: TurnResult


class TurnYielded(Value):
    """Plumbing of the agent pipeline: core recorded the wait a turn ended with."""

    kind: Literal["turn_yielded"] = "turn_yielded"
    continuation: Continuation


class YieldFailed(Value):
    """Plumbing of the agent pipeline: core could not publish the wait it recorded."""

    kind: Literal["yield_failed"] = "yield_failed"
    failure: YieldPublicationFailed


class ResumeReady(Value):
    """Plumbing of the agent pipeline: a suspended turn may be resumed."""

    kind: Literal["resume_ready"] = "resume_ready"
    authorization: ResumeAuthorized


class ResumeFailed(Value):
    """Plumbing of the agent pipeline: core will never resume a suspended turn."""

    kind: Literal["resume_failed"] = "resume_failed"
    refusal: ResumeRefused


class CheckpointKnown(Value):
    """Plumbing of the agent pipeline: core recorded or declined a turn's checkpoint."""

    kind: Literal["checkpoint_known"] = "checkpoint_known"
    invocation: InvocationRef
    checkpoint: Checkpoint


class MeasurementFinished(Value):
    """A measurement ended with evidence, or a typed failure and partial evidence."""

    kind: Literal["measurement_finished"] = "measurement_finished"
    decision: DecisionId | None
    result: MeasurementResult
    comparison: Comparison | None = Field(default=None, exclude_if=unset)
    """The measured candidate compared with its base over every sample held when it ended.

    None when the candidate has no base's samples (a task without a benchmark stage, a base
    that could not be measured, or a run no sample came of)."""


class OperationFinished(Value):
    """A registered operation ended with its outcome."""

    kind: Literal["operation_finished"] = "operation_finished"
    decision: DecisionId
    result: OperationResult


class AdoptionFinished(Value):
    """Adoption of a proposed winner ended with a verified revision or a typed failure."""

    kind: Literal["adoption_finished"] = "adoption_finished"
    decision: DecisionId | None
    result: AdoptionResult | AdoptionFailed


class StopRequested(Value):
    """An operator asked the run to stop, steer, pause or resume."""

    kind: Literal["stop_requested"] = "stop_requested"
    control: ControlInput


class StopLifted(Value):
    """An explicit resume lifted the stop that stood: the run goes on, as if not stopped.

    Work the stop already ended stays ended (a task that took ``Stopped`` has returned); a
    task that had not taken it yet no longer will, and ``ctx.stop_requested`` is false again.
    """

    kind: Literal["stop_lifted"] = "stop_lifted"
    control: ControlInput
    new_epoch: bool = Field(default=False, exclude_if=lambda value: not value)


class TimeReached(Value):
    """The run clock reached a time the search asked to be woken at."""

    kind: Literal["time_reached"] = "time_reached"
    time: Seconds


class MessageDelivered(Value):
    """A queued message reached the session it was addressed to (opt-in)."""

    kind: Literal["message_delivered"] = "message_delivered"
    delivery: InputDelivered


class MessageDropped(Value):
    """A queued message can no longer reach its session (opt-in)."""

    kind: Literal["message_dropped"] = "message_dropped"
    drop: InputDropped


class Interrupted(Value):
    """An interruption ended with a checkpoint, or failed to produce one (opt-in)."""

    kind: Literal["interrupted"] = "interrupted"
    result: InterruptCompleted | InterruptFailed


type Event = Annotated[
    WorkspaceOpened
    | WorkspaceClosed
    | MeasurementFinished
    | OperationFinished
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
"""Everything core reports to a run's flows. The flow runtime routes each to what waits for it."""


class AttemptSummary(Value):
    """What a search may read of one workspace (attempt): its identity, phase and results.

    Request ids, receipts, the release graph and checkpoint bookkeeping are core's and are
    not here; a search reaches work only through its own decisions.
    """

    attempt: AttemptRef
    item_id: ItemId
    phase: AttemptPhase
    workspace: WorkspacePlan
    budget: AttemptBudget
    checkpoint: RevisionRef | None
    evaluation_history: AttemptEvaluationHistory
    terminal_reason: AttemptTerminalReason | None

    @classmethod
    def of(cls, view: AttemptView) -> AttemptSummary:
        """The summary of core's view of an attempt."""
        return cls(
            attempt=AttemptRef(attempt_id=view.attempt_id, generation=view.generation),
            item_id=view.item_id,
            phase=view.phase,
            workspace=view.workspace,
            budget=view.budget,
            checkpoint=view.checkpoint,
            evaluation_history=view.evaluation_history,
            terminal_reason=view.terminal_reason,
        )


class RunView(Value):
    """What a search may read about the run, and nothing it could write.

    The facts here belong to other owners. A search reads them when it needs them and
    keeps none of them in its own state.
    """

    run: Scope
    """The run's own scope (what ``THIS_RUN`` stands for in a decision)."""
    now_at: Seconds
    deadline_at: Seconds
    facts: RunFacts
    capabilities: Capabilities
    limits: Limits
    attempts: tuple[AttemptSummary, ...]
    measurements: tuple[EvidenceRef, ...]
    settlements: tuple[Settlement, ...]
    comparisons: tuple[StatedComparison, ...] = ()
    """Every candidate input's comparison with its base as it stands now, and whether it is
    still settling; one per scope, candidate input and base input."""


class DiscardReason(StrEnum):
    """Why core kept an event from the search. Closed."""

    INTERNAL_FAILURE = "internal-failure"
    """A request core made for itself ended without a result; the search never asked for it."""

    LATE_RESULT = "late-result"
    """A result arrived for a request that had already ended with a failure."""


class Discarded(Value):
    """An event core handled instead of delivering, kept so the shell can log it.

    ``failure`` is set for an internal failure; ``request`` and ``event`` for a late result.
    Typed fields only: nothing here is free text.
    """

    reason: DiscardReason
    failure: Failed | None = None
    request: RequestId | None = None
    event: EventKind | None = None
