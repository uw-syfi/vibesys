"""The orchestrator's handoff file: what one planning call leaves for the planners after it.

Each planning call starts a new planner session, so a planner remembers nothing of the calls
before it. A call that schedules a plan asks the handoff owner to write one file for it, in the
run's own state: the reply's reasoning, the workstreams it scheduled, and the planner's note
(`HandoffNote`, from the reply's `PlannerHandoff`: what it expects of each workstream, what it would do next on each outcome, its
open doubts). The planner's reply is the one source of what the file says; the owner renders it
from a template and answers with the file's agent-visible path. Later planning prompts list those
paths, newest first, and never their contents: the planner reads the files it needs itself.

The write is keyed by the planning call's number, so a replay writes the same file again with
the same bytes, and a write whose wait timed out or was lost is sent once more (a keyed write may
be). The paths come back as operation outcomes, which a restart replays, so the list survives a
restart and a resume; each outcome lists every file the owner holds, so a file that landed under
a failed write is listed once a later write succeeds.
"""

from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from vibesys.orchestration.dynamic.search._plan import PlanCheck, PlanUpdate
from vibesys.orchestration.dynamic.search._values import HandoffNote, MergeRecord, WorkPlan
from vs_core.api.search import (
    LifecycleClass,
    OperationDescriptor,
    OperationRegistration,
    OperationRequest,
    SchemaRef,
    Value,
)

WRITE_HANDOFF_KIND = "dynamic.write_handoff"


class HandoffWritten(Value):
    """The handoff file the owner wrote, and every handoff file the run's state holds now.

    ``listed`` comes from the owner's store, not from the search's memory, so a file whose own
    write was reported failed after its bytes landed is still listed from the next write on.
    """

    status: Literal["succeeded"] = "succeeded"
    path: str = Field(min_length=1)
    """Relative to the planner's workspace, the run's project checkout."""
    listed: tuple[str, ...] = Field(min_length=1)
    """Every handoff file of the run, oldest call first; ``path`` among them."""


class WriteHandoff(OperationRequest):
    """Write the handoff file of one planning call; writing it again writes the same bytes."""

    kind: Literal["dynamic.write_handoff"] = WRITE_HANDOFF_KIND
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = HandoffWritten
    call: int = Field(ge=1)
    """The planning call's number: the file's identity, one file per call."""
    reasoning: str
    scheduled: tuple[WorkPlan, ...]
    updates: tuple[PlanUpdate, ...] = ()
    handoff: HandoffNote
    merges: tuple[MergeRecord, ...] = ()
    """Every merge finished by the time of the call, oldest first."""


HANDOFF = OperationRegistration(
    descriptor=OperationDescriptor(
        kind=WRITE_HANDOFF_KIND,
        request_schema=SchemaRef(name="dynamic.write-handoff", version=1),
        outcome_schema=SchemaRef(name="dynamic.write-handoff-outcome", version=1),
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
        inspect=True,
    ),
    request_model=WriteHandoff,
    outcome_model=HandoffWritten,
)
"""The handoff write, an operation the host's handoff owner performs for the search."""


def handoff_request(
    call: int, check: PlanCheck, merges: tuple[MergeRecord, ...] = ()
) -> WriteHandoff:
    """The write a scheduled plan asks for."""
    return WriteHandoff(
        call=call,
        reasoning=check.reasoning,
        scheduled=check.accepted,
        updates=check.updates,
        handoff=check.handoff,
        merges=merges,
    )
