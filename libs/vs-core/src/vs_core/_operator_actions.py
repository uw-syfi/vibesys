"""The operator's typed actions on core entities: what each one asks core to do.

An operator action is a control (``ControlInput``) that core applies the same way in every
search, through the lifecycle a search's own decisions use:

- ``cancel_attempt`` withdraws the attempt with a ``Cancel`` disposition, as a search would;
  the search hears the attempt close cancelled (``WorkspaceClosed``).
- ``cancel_item`` does that for every running attempt at the item, and core refuses every
  later ``StartAttempt`` at the item. An attempt already settling finishes, and its result
  stands (``ItemCancellation``).
- ``set_max_parallel`` changes the limit core's admission holds the run's attempts to. A
  lower limit admits nothing new until enough attempts end; it stops none that runs.

The run's recorded controls are the one record of each: the cancelled items and the limit in
force are read from them here, so core keeps no second copy. Each action leaves a notice
(``ControlInput.artifact``) that core delivers as run-scoped input at the next agent turn.
Pure: it reads state and returns values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._registry import ContractError
from .types.attempts import AttemptPhase
from .types.common import (
    OPERATOR_ACTIONS,
    ActionRefusal,
    AttemptRef,
    DecisionId,
    InputId,
    InputMode,
    Scope,
    ScopeInputTarget,
)
from .types.session_inputs import SessionInput
from .types.sessions import SessionInputReceived
from .types.strategy import Cancel, Withdraw

if TYPE_CHECKING:
    from .types.attempts import AttemptView
    from .types.common import AttemptId, ControlInput, ItemId
    from .types.kernel import CoreState, RunState

_ENDED = frozenset({AttemptPhase.CLOSING, AttemptPhase.TERMINAL})
_BLOCKED = frozenset({AttemptPhase.BLOCKED})


def parallel_limit(run: RunState) -> int:
    """How many attempts core admits at once: the operator's latest limit, else the run's."""
    limit = run.limits.max_parallel
    for control in run.controls:
        if control.action == "set_max_parallel" and control.max_parallel is not None:
            limit = control.max_parallel
    return limit


def cancelled_items(run: RunState) -> frozenset[ItemId]:
    """The work items the operator cancelled: core starts no attempt at them."""
    return frozenset(
        control.item
        for control in run.controls
        if control.action == "cancel_item" and control.item is not None
    )


def _latest(state: CoreState, attempt_id: object) -> AttemptView | None:
    latest: AttemptView | None = None
    for row in state.attempts.attempts:
        if row.attempt_id == attempt_id and (latest is None or row.generation > latest.generation):
            latest = row
    return latest


def _attempts_of(state: CoreState, item: ItemId) -> tuple[AttemptView, ...]:
    """The latest generation of each attempt at ``item``, by attempt id."""
    latest: dict[str, AttemptView] = {}
    for row in state.attempts.attempts:
        if row.item_id == item:
            held = latest.get(row.attempt_id.root)
            if held is None or row.generation > held.generation:
                latest[row.attempt_id.root] = row
    return tuple(row for _, row in sorted(latest.items()))


@dataclass(frozen=True)
class ItemCancellation:
    """What ``cancel_item`` does to each attempt at the item that has not ended.

    ``stopped`` attempts run or wait to run: core withdraws each with a ``Cancel``
    disposition. ``parked`` attempts (blocked) run nothing to stop. ``settling`` attempts
    were already closing: they finish, and their results stand.
    """

    stopped: tuple[AttemptId, ...]
    parked: tuple[AttemptId, ...]
    settling: tuple[AttemptId, ...]


def item_cancellation(state: CoreState, item: ItemId) -> ItemCancellation:
    """What cancelling ``item`` in ``state`` does to each of its attempts."""
    rows = _attempts_of(state, item)
    return ItemCancellation(
        stopped=tuple(row.attempt_id for row in rows if row.phase not in _ENDED | _BLOCKED),
        parked=tuple(row.attempt_id for row in rows if row.phase in _BLOCKED),
        settling=tuple(row.attempt_id for row in rows if row.phase == AttemptPhase.CLOSING),
    )


def action_refusal_kind(state: CoreState, control: ControlInput) -> ActionRefusal | None:
    """Why core refuses the operator action ``control`` in ``state``, or None if it takes it.

    Core's ``control_refusal`` asks this for an operator action, after "a terminal run takes
    no control". An attempt or item core does not hold, an attempt that has ended or is
    blocked, and an item already cancelled are refused, since the action could change nothing; so is a parallel limit above the run's launch limit, which sized the
    host. Anything else is taken.
    """
    match control.action:
        case "cancel_attempt":
            return _attempt_refusal(_latest(state, control.attempt))
        case "cancel_item":
            if control.item in cancelled_items(state.run):
                return ActionRefusal.ALREADY_CANCELLED
            if not any(row.item_id == control.item for row in state.attempts.attempts):
                return ActionRefusal.NO_SUCH_SUBJECT
        case "set_max_parallel":
            limit = control.max_parallel
            if limit is not None and limit > state.run.limits.max_parallel:
                return ActionRefusal.ABOVE_LAUNCH_LIMIT
    return None


def _attempt_refusal(latest: AttemptView | None) -> ActionRefusal | None:
    if latest is None:
        return ActionRefusal.NO_SUCH_SUBJECT
    if latest.phase in _ENDED:
        return ActionRefusal.ENDED
    if latest.phase in _BLOCKED:
        return ActionRefusal.PARKED
    return None


def cancellations(state: CoreState, control: ControlInput) -> tuple[Withdraw, ...]:
    """The withdrawals that carry out a cancel action, one per attempt it cancels.

    Each is a ``Withdraw`` with a ``Cancel`` disposition under the run's own scope and a
    decision identity derived from the control's, so a replay names the same decisions.
    """
    match control.action:
        case "cancel_attempt":
            latest = _latest(state, control.attempt)
            targets = () if latest is None or latest.phase in _ENDED | _BLOCKED else (latest,)
        case "cancel_item" if control.item is not None:
            stopped = item_cancellation(state, control.item).stopped
            targets = tuple(
                row for row in _attempts_of(state, control.item) if row.attempt_id in stopped
            )
        case _:
            targets = ()
    scope = Scope(owner=state.run.run_id, generation=state.run.generation)
    return tuple(
        Withdraw(
            decision_id=DecisionId(root=f"control:{control.control_id.root}:{row.attempt_id.root}"),
            scope=scope,
            target=AttemptRef(attempt_id=row.attempt_id, generation=row.generation),
            disposition=Cancel(),
        )
        for row in targets
    )


def notice_input_id(control: ControlInput) -> InputId:
    """The input the notice of the operator action ``control`` becomes."""
    return InputId(root=f"notice-{control.control_id.root}")


def notice(state: CoreState, control: ControlInput, now_at: float) -> SessionInputReceived:
    """The run-scoped next-turn input that tells the run's agents what ``control`` changed.

    ``control`` must be an operator action with its notice; its place among the run's
    controls orders it with the steers.
    """
    if control.action not in OPERATOR_ACTIONS or control.artifact is None:
        message = "only an operator action with its notice leaves one"
        raise ContractError(("control", "artifact"), message)
    position = next(
        (
            index
            for index, held in enumerate(state.run.controls, start=1)
            if held.control_id == control.control_id
        ),
        len(state.run.controls) + 1,
    )
    run = Scope(owner=state.run.run_id, generation=state.run.generation)
    return SessionInputReceived(
        input=SessionInput(
            input_id=notice_input_id(control),
            target=ScopeInputTarget(scope=run),
            artifact=control.artifact,
            received_at=now_at,
            sequence=position,
            mode=InputMode.NEXT_TURN,
        )
    )
