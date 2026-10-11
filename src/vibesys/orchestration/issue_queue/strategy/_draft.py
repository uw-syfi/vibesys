"""The issue-queue binding of the shared authoring kit (`vs_core.api`) to its config and ids.

The kit's `Draft` and builders take every deadline and identity as an argument; this module
supplies the issue queue's: the operation deadline from `IssueQueueConfig`, the `plain`
identity scheme, and the keys that make a board write findable after a restart.
"""

from dataclasses import dataclass

from vibesys.orchestration.issue_queue.strategy._config import IssueQueueConfig
from vibesys.orchestration.issue_queue.strategy._ids import IDS
from vibesys.orchestration.issue_queue.strategy._operations import RenderRoleArtifacts
from vibesys.orchestration.issue_queue.strategy._prompts import PromptContext
from vibesys.orchestration.issue_queue.strategy._state import (
    IssueQueueStrategyState,
    Job,
    Role,
    TurnRecord,
)
from vs_core.api import (
    DecisionId,
    Operation,
    OperationRequest,
    RequestTurn,
    RunView,
    Scope,
    TurnShape,
    component,
    declared_operation,
)
from vs_core.api import Draft as AuthoringDraft
from vs_core.api import invocation_for as _invocation_for
from vs_core.api import request_turn as _request_turn
from vs_core.api import turn_decision_id as _turn_decision_id


@dataclass(kw_only=True)
class Draft(AuthoringDraft[IssueQueueStrategyState]):
    """The state and decisions one issue-queue `decide` call is building."""

    config: IssueQueueConfig


def operation(
    draft: Draft, identifier: DecisionId, scope: Scope, request: OperationRequest
) -> Operation:
    """A declared operation due before the run's own deadline."""
    return declared_operation(
        draft.view, identifier, scope, request, seconds=draft.config.operation_seconds
    )


def key_of(job: Job) -> str:
    """Stable raw subject of one job; the id constructors encode it."""
    return f"{job.kind.value}.{job.sequence}"


def subject_of(job: Job, role: Role) -> str:
    """Decision subject of one role's turns inside a job."""
    return f"{key_of(job)}.{role.value}"


def change_key(view: RunView, job: Job | None, what: str) -> str:
    """The key of one board change: unique to this run, job and fact.

    The tracker stores it with the issue or event it writes, which is how a replayed write
    is recognized as already landed. The run identity keeps two runs on one GitHub
    repository from recognizing each other's writes.
    """
    scope = "run" if job is None else key_of(job)
    return f"{view.run.run_id.root}/{scope}/{what}"


def render_request(subject: str, turn: TurnRecord, context: PromptContext) -> RenderRoleArtifacts:
    """Ask the renderer for the artifacts of one role turn."""
    return RenderRoleArtifacts(subject=component(subject), ordinal=turn.serial, context=context)


def render_id(subject: str, turn: TurnRecord) -> DecisionId:
    """Decision ID of the prompt render preceding one turn."""
    return IDS.decision_id("render", subject, turn.serial)


def turn_id(subject: str, turn: TurnRecord) -> DecisionId:
    """Decision ID of one turn request."""
    return _turn_decision_id(IDS, subject, turn)


def invocation_of(role: Role, subject: str, turn: TurnRecord) -> str:
    """The logical invocation ID of a turn; a resume reuses the ID core authorized."""
    return _invocation_for(IDS, role.value, subject, turn).root


def request_turn(draft: Draft, shape: TurnShape, turn: TurnRecord, scope: Scope) -> RequestTurn:
    """Build the paid, free, correction or resume turn the record describes."""
    return _request_turn(draft.view, IDS, shape, turn, scope)
