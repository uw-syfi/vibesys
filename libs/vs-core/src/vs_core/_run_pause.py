"""Who holds a paused run, and which controls core takes in the run's state.

A run's pause is a fact of its recorded controls, so core keeps no second copy of it:
``run_pause`` folds the controls in order. An operator pause holds the run and drops a
capacity wait the operator asked for earlier. A quota stop pauses a run that is not paused
yet and leaves any other pause as it is. A capacity wait marks the pause it is asked
under, and any immediate resume ends the pause.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._registry import ContractError
from .types.common import PauseOwner, RunPause, RunStatus

if TYPE_CHECKING:
    from .types.common import ControlInput
    from .types.kernel import RunState


def run_pause(run: RunState) -> RunPause | None:
    """The pause the run's controls hold it in; None unless the run is paused."""
    if run.status != RunStatus.PAUSED:
        return None
    pause: RunPause | None = None
    for control in run.controls:
        pause = _after(pause, control)
    return pause


def _after(pause: RunPause | None, control: ControlInput) -> RunPause | None:
    match control.action:
        case "pause" if control.owner is PauseOwner.OPERATOR:
            return RunPause(owner=PauseOwner.OPERATOR)
        case "pause":
            return pause or RunPause(owner=PauseOwner.QUOTA)
        case "resume" if control.when == "capacity_returns":
            return None if pause is None else pause.model_copy(update={"capacity_wait": True})
        case "resume" | "stop":
            return None
        case _:
            return pause


def control_refusal(run: RunState, control: ControlInput) -> ContractError | None:
    """Why core refuses a new ``control`` for ``run`` as it stands, or None if it takes it.

    The one statement of the rule: core raises the error when the control arrives, and a
    shell that must answer an operator before core commits asks the same question. A stop is
    never refused.
    """
    if control.action == "stop":
        return None
    if run.status == RunStatus.TERMINAL:
        return ContractError(("run", "status"), "terminal run rejects controls")
    if control.action == "steer":
        return None
    if run.status in (RunStatus.CLOSING, RunStatus.BLOCKED) or run.result is not None:
        return ContractError(("run", "cleanup"), "cleanup must finish before pause or resume")
    if control.when == "capacity_returns" and run.status != RunStatus.PAUSED:
        return ContractError(("control", "when"), "only a paused run waits for capacity")
    return None
