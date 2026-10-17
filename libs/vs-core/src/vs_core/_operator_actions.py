"""The operator's typed actions on core entities: what each one asks core to do.

An operator action is a control (``ControlInput``) that core applies the same way in every
search, through the lifecycle a search's own decisions use:

- ``cancel_attempt`` withdraws the attempt with a ``Cancel`` disposition, as a search would;
  the search hears the attempt close cancelled (``WorkspaceClosed``).
- ``cancel_item`` does that for every attempt at the item that has not ended, and core
  refuses every later ``StartAttempt`` at the item.
- ``set_max_parallel`` changes the limit core's admission holds the run's attempts to. A
  lower limit admits nothing new until enough attempts end; it stops none that runs.

The run's recorded controls are the one record of each: the cancelled items and the limit in
force are read from them here, so core keeps no second copy. Each action leaves a notice
(``ControlInput.artifact``) that core delivers as run-scoped input at the next agent turn.
Pure: it reads state and returns values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._registry import ContractError
from .types.attempts import AttemptPhase
from .types.common import (
    OPERATOR_ACTIONS,
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
    from .types.common import ControlInput, ItemId
    from .types.kernel import CoreState, RunState

_ENDED = frozenset({AttemptPhase.CLOSING, AttemptPhase.TERMINAL})


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
    rows = [row for row in state.attempts.attempts if row.attempt_id == attempt_id]
    return max(rows, key=lambda row: row.generation, default=None)


def _live_attempts_of(state: CoreState, item: ItemId) -> tuple[AttemptView, ...]:
    latest: dict[str, AttemptView] = {}
    for row in state.attempts.attempts:
        if row.item_id == item:
            held = latest.get(row.attempt_id.root)
            if held is None or row.generation > held.generation:
                latest[row.attempt_id.root] = row
    return tuple(row for _, row in sorted(latest.items()) if row.phase not in _ENDED)


def action_refusal(state: CoreState, control: ControlInput) -> ContractError | None:
    """Why core refuses the operator action ``control`` in ``state``, or None if it takes it.

    The one statement of the rule, after ``control_refusal``: core raises the error when the
    action arrives, and a shell that must answer an operator first asks the same question.
    An attempt or item core does not hold, an attempt that has ended, and an item already
    cancelled are refused, since the action could change nothing.
    """
    match control.action:
        case "cancel_attempt":
            latest = _latest(state, control.attempt)
            if latest is None:
                return ContractError(("control", "attempt"), "the run holds no such attempt")
            if latest.phase in _ENDED:
                return ContractError(("control", "attempt"), "the attempt has ended")
        case "cancel_item":
            if control.item in cancelled_items(state.run):
                return ContractError(("control", "item"), "the item is already cancelled")
            if not any(row.item_id == control.item for row in state.attempts.attempts):
                return ContractError(("control", "item"), "the run holds no such item")
        case _:
            return None
    return None


def cancellations(state: CoreState, control: ControlInput) -> tuple[Withdraw, ...]:
    """The withdrawals that carry out a cancel action, one per attempt it cancels.

    Each is a ``Withdraw`` with a ``Cancel`` disposition under the run's own scope and a
    decision identity derived from the control's, so a replay names the same decisions.
    """
    match control.action:
        case "cancel_attempt":
            latest = _latest(state, control.attempt)
            targets = () if latest is None or latest.phase in _ENDED else (latest,)
        case "cancel_item" if control.item is not None:
            targets = _live_attempts_of(state, control.item)
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
