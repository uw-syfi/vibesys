"""Validate that a dispatch is authorized to resume: its episode, its turn and its history.

A resume dispatch needs the exact published successor proof, and a run writer needs the exact
predecessor checkpoint. These are pure checks of the state against one request.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ._continuations import (
    SuspensionRefusal,
    exhausted,
    paid_prefix_refusal,
    published_history,
    resume_refusal,
)
from ._dispatch_episode import (
    dispatch_episode_current,
)
from ._proofs import (
    Proven,
    accepted_receipt_for,
    descriptor_matches,
    operation_for,
)
from ._registry import ContractError
from .types.common import (
    AttemptId,
    DecisionId,
    InvocationRef,
    LifecycleClass,
    OperationNormalizationKind,
)
from .types.evaluation import (
    ContinuationPhase,
)
from .types.kernel import (
    AreaChange,
    CoreState,
    Signal,
)
from .types.records import (
    ExecuteRegisteredOperation,
    Request,
)
from .types.sessions import (
    Access,
    DispatchTurn,
    Invocation,
    ResumeSessionTurn,
    TurnSpec,
)
from .types.strategy import (
    Accepted,
    RequestTurn,
)

if TYPE_CHECKING:
    from .types.evaluation_history import EvaluationHistoryCursor

MAX_SIGNALS = 1024

type Dispatch = Callable[[CoreState, Signal], AreaChange]
type _Pending = tuple[Signal, DecisionId | None, DecisionId | None]


def validate_dispatch_episode(state: CoreState, request: Request) -> None:
    """A prepared ordinary mutation never gains authority over a later episode."""
    if not dispatch_episode_current(state, request):
        raise ContractError(
            ("admission_id",), "ordinary mutation requires the current owned admission episode"
        )


def _registered_session_turn(state: CoreState, request: ExecuteRegisteredOperation) -> TurnSpec:
    """Resolve dispatch authority from the accepted canonical registered payload."""
    origin = operation_for(state.run.receipts, request)
    declaration = descriptor_matches(
        state.registry,
        state.run.capabilities,
        request.operation,
        LifecycleClass.SESSION_TURN,
        OperationNormalizationKind.NONE,
    )
    if (
        not isinstance(origin, Proven)
        or not isinstance(declaration, Proven)
        or origin.value.normalized_turn is None
    ):
        raise ContractError(
            ("decision_id",), "registered session dispatch requires canonical turn proof"
        )
    return origin.value.normalized_turn


def _builtin_session_turn(state: CoreState, request: DispatchTurn | ResumeSessionTurn) -> TurnSpec:
    """Resolve dispatch authority before classifying a caller-supplied turn.

    Paid and correction lifecycle policy remains owned by Sessions. A request
    cannot erase its decision origin or downgrade a resume to bypass proof fences.
    """
    origin = accepted_receipt_for(state.run.receipts, request.decision_id, None)
    receipt = origin.value if isinstance(origin, Proven) else None
    decision = receipt.decision if receipt is not None else None
    if (
        receipt is None
        or not isinstance(receipt.feedback, Accepted)
        or receipt.feedback.decision_id != receipt.decision_id
        or not isinstance(decision, RequestTurn)
        or decision.decision_id != receipt.decision_id
        or request.request_id not in receipt.request_ids
        or decision.scope != request.scope
        or decision.turn != request.turn
    ):
        raise ContractError(
            ("decision_id",), "builtin session dispatch requires canonical turn proof"
        )
    return decision.turn


def validate_resume_authority(state: CoreState, request: Request) -> None:
    """Missing history/publication/checkpoint values never authorize a resume.

    These are shared proof fences. Evaluation B owns scientific exhaustion and
    timeout policy, while Sessions A owns charge, lease and checkpoint issuance.
    """
    if isinstance(request, DispatchTurn | ResumeSessionTurn):
        turn = _builtin_session_turn(state, request)
    elif (
        isinstance(request, ExecuteRegisteredOperation)
        and request.operation.schema_ref.lifecycle == LifecycleClass.SESSION_TURN
    ):
        turn = _registered_session_turn(state, request)
    else:
        return
    if turn.charge_class != "resume":
        if isinstance(request, ResumeSessionTurn):
            raise ContractError(("turn", "charge_class"), "resume request requires resume charge")
        return
    continuation = next(
        (
            row
            for row in state.evaluation.continuations
            if row.continuation_id == turn.continuation_id
        ),
        None,
    )
    successor = InvocationRef(
        session_id=turn.session.session_id,
        invocation_id=turn.invocation_id,
        generation=request.scope.generation,
    )
    receipt = continuation.authorization_receipt if continuation is not None else None
    preceding = next(
        (
            row
            for row in state.sessions.invocations
            if continuation is not None and row.invocation == continuation.invocation
        ),
        None,
    )
    if (
        continuation is None
        or receipt is None
        or preceding is None
        or preceding.scope != request.scope
        or preceding.turn.session != turn.session
        or continuation.invocation.session_id != successor.session_id
        or continuation.invocation == successor
        or receipt.continuation_id != turn.continuation_id
        or receipt.next_invocation != successor
        or receipt.timeout != continuation.timeout
        or continuation.next_invocation != successor
        or continuation.invocation.generation != request.scope.generation
        or continuation.phase not in (ContinuationPhase.AUTHORIZED, ContinuationPhase.RESUMED)
        or (
            isinstance(request, ResumeSessionTurn)
            and request.continuation_id != turn.continuation_id
        )
    ):
        raise ContractError(
            ("authorization_receipt",), "resume requires exact published successor proof"
        )
    _validate_resume_owner(state, request, turn, preceding, receipt.history_cursor)


def _validate_resume_owner(
    state: CoreState,
    request: Request,
    turn: TurnSpec,
    preceding: Invocation,
    publication_cursor: EvaluationHistoryCursor | None,
) -> None:
    """History belongs to the current attempt; run writer proof names its predecessor."""
    if isinstance(request.scope.owner, AttemptId):
        owner = next(
            (
                row
                for row in state.attempts.attempts
                if row.attempt_id == request.scope.owner
                and row.generation == request.scope.generation
            ),
            None,
        )
        if owner is None or exhausted(state.attempts, request.scope):
            raise ContractError(
                ("evaluation_history",), "resume requires complete unexhausted attempt history"
            )
        prefix = preceding.evaluation_prefix
        history = owner.evaluation_history
        # Coverage only grows, so the paid-cycle prefix is checked on the stored history.
        if prefix is None or paid_prefix_refusal(history, prefix) is not None:
            raise ContractError(("evaluation_prefix",), SuspensionRefusal.PREFIX_MISMATCH.detail)
        if (
            publication_cursor is None
            or publication_cursor.ordinal < prefix.ordinal
            or publication_cursor.ordinal > len(history.covered_submissions)
            or (
                publication_cursor.ordinal
                and history.covered_submissions[publication_cursor.ordinal - 1]
                != publication_cursor.submission_id
            )
        ):
            raise ContractError(
                ("authorization_receipt", "history_cursor"),
                "resume requires exact publication history prefix after paid-cycle start",
            )
        # The rule authorization applied, to the history it published (``published_history``).
        refusal = resume_refusal(published_history(history, publication_cursor), prefix)
        if refusal is not None:
            raise ContractError(
                ("evaluation_history",), "resume requires complete unexhausted attempt history"
            )
    else:
        if (
            request.scope.owner != state.run.run_id
            or request.scope.generation != state.run.generation
        ):
            raise ContractError(("scope",), "resume requires current run generation")
        if turn.session.access == Access.WRITE_CANDIDATE and not any(
            proof.scope == request.scope and proof.invocation == preceding.invocation
            for proof in state.sessions.run_checkpoints
        ):
            raise ContractError(
                ("run_checkpoints",), "run writer resume requires exact predecessor checkpoint"
            )
