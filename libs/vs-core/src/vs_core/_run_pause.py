"""Who holds a paused run, and which controls core takes in the run's state.

A run's pause is a fact of its recorded controls, so core keeps no second copy of it:
``run_pause`` folds the controls in order. An operator pause holds the run and drops a
capacity wait the operator asked for earlier. A quota stop pauses a run that is not paused
yet and leaves any other pause as it is. A capacity wait marks the pause it is asked
under, and any immediate resume ends the pause.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from ._operator_actions import action_refusal_kind
from ._registry import ContractError
from .types.common import ACTION_REFUSAL_TEXT, OPERATOR_ACTIONS, PauseOwner, RunPause, RunStatus
from .types.strategy import Finish

if TYPE_CHECKING:
    from .types.common import ActionRefusal, ControlInput
    from .types.kernel import CoreState, RunState


DEADLINE_STOP: Final = "deadline"
"""The identity of the stop the host delivers when the run's deadline passes.

The deadline ends the current epoch after its time budget is spent.
"""

ENVIRONMENT_STOP: Final = "run-environment"
"""The identity of the stop the host delivers when the run environment can run none of the run's
work (a local Slurm root its probe could not prove shared).

The host ends this epoch as failed. Resume can try a successor epoch. The host
records operators' controls under ``op-`` and ``control-`` identities.
"""


class ControlRefusedError(ContractError):
    """Core refuses ``control`` in the run's state as it stands (``control_refusal``).

    Only this rule raises it, so a shell can tell a refusal, which an operator hears about
    and the run outlives, from a malformed control (an identity conflict, an undeclared
    steer target), which is a defect in whatever built the control.
    """

    def __init__(
        self,
        control: ControlInput,
        path: tuple[str | int, ...],
        detail: str,
        *,
        action_refusal: ActionRefusal | None = None,
    ) -> None:
        """Name the refused control, where and why core refuses it, and an action's reason."""
        self.control = control
        self.action_refusal = action_refusal
        """Why an operator action is refused, as a closed set; None for any other control."""
        super().__init__(path, detail)


def run_pause(run: RunState) -> RunPause | None:
    """The pause the run's controls hold it in; None unless the run is paused."""
    if run.status != RunStatus.PAUSED:
        return None
    pause: RunPause | None = None
    for control in run.controls:
        pause = pause_after(pause, control)
    return pause


def pause_after(pause: RunPause | None, control: ControlInput) -> RunPause | None:
    """The pause ``control`` leaves the run in when it was in ``pause``; one step of the fold."""
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


def capacity_wait_stands(run: RunState) -> bool:
    """Whether the operator's wait for capacity stands for the run's next quota stop.

    A resume that keeps the wait (``ControlInput.capacity_wait_stands``, which a restart
    records) makes it stand while the run runs; a quota stop leaves it standing until the
    stopped turn takes it as the pause's own wait (a ``capacity_returns`` resume). Any other
    resume, an operator pause or a stop ends it.
    """
    stands = False
    for control in run.controls:
        stands = stands_after(control, stood=stands)
    return stands


def stands_after(control: ControlInput, *, stood: bool) -> bool:
    """Whether the wait stands after ``control``, given whether it ``stood`` before; one fold step."""
    match control.action:
        case "resume":
            return control.capacity_wait_stands
        case "pause" if control.owner is PauseOwner.QUOTA:
            return stood
        case "pause" | "stop":
            return False
        case _:
            return stood


def control_refusal(state: CoreState, control: ControlInput) -> ControlRefusedError | None:
    """Validate a control against current admission, cleanup, and epoch ownership.

    Every terminal result may resume. A drained epoch resumes only after a new writer
    has acquired it, and cleanup must settle before a successor can admit work.
    """
    run = state.run
    if run.status in (RunStatus.DRAINING, RunStatus.PARKED, RunStatus.TERMINAL):
        return _inactive_refusal(state, control)
    if control.action in OPERATOR_ACTIONS:
        return _action_refusal(state, control)
    if control.action in ("stop", "steer", "withdraw"):
        return None
    return _pause_refusal(run, control)


def _inactive_refusal(state: CoreState, control: ControlInput) -> ControlRefusedError | None:
    run = state.run
    if (
        control.action in ("steer", "withdraw")
        and run.status in (RunStatus.DRAINING, RunStatus.PARKED)
    ) or (run.status == RunStatus.DRAINING and control.action == "pause_now"):
        return None
    if run.status in (RunStatus.DRAINING, RunStatus.PARKED):
        if (
            run.status == RunStatus.PARKED
            and control.action == "resume"
            and control.when == "now"
            and run.park is not None
            and state.records.recovery.epoch > run.park.epoch
        ):
            return None
        return ControlRefusedError(control, ("run", "status"), "pause is terminal for this epoch")
    if run.status == RunStatus.TERMINAL:
        if control.action == "resume" and control.when != "capacity_returns":
            return None
        return ControlRefusedError(
            control, ("run", "status"), "the epoch ended; Resume starts another"
        )
    return None


def _action_refusal(state: CoreState, control: ControlInput) -> ControlRefusedError | None:
    kind = action_refusal_kind(state, control)
    if kind is None:
        return None
    field = {"cancel_attempt": "attempt", "cancel_item": "item"}.get(control.action, "max_parallel")
    return ControlRefusedError(
        control, ("control", field), ACTION_REFUSAL_TEXT[kind], action_refusal=kind
    )


def stop_standing(run: RunState) -> bool:
    """Whether the latest closing control has not been lifted by a subsequent Resume."""
    standing = False
    for control in run.controls:
        if control.action == "stop":
            standing = True
        elif control.action == "resume":
            standing = False
    return standing


def reopened(run: RunState) -> RunState:
    """The run a successor epoch reopened: running, with no ending committed.

    The ending's ``Finish`` leaves the receipts: it is no longer committed, and the run's next
    ending is its first (`committed_stop` keeps the first Finish). Every other receipt stays as
    core answered it. What the ending cleaned up stays cleaned up (its sessions are closed, its
    jobs cancelled, its attempts settled): the search starts the work that remains afresh.
    """
    ended = run.result
    kept = tuple(
        receipt
        for receipt in run.receipts
        if not (isinstance(receipt.decision, Finish) and receipt.decision.result == ended)
    )
    return run.model_copy(
        update={"status": RunStatus.RUNNING, "result": None, "receipts": kept, "park": None}
    )


def _pause_refusal(run: RunState, control: ControlInput) -> ControlRefusedError | None:
    lifts = control.action == "resume" and control.when != "capacity_returns"
    standing = stop_standing(run)
    if standing and not lifts:
        # A pause would change nothing an operator could see before the search ends the run;
        # only an explicit resume (which lifts the stop) is taken.
        return ControlRefusedError(
            control, ("run", "controls"), "a stopped run takes no pause, only a resume"
        )
    if run.status in (RunStatus.CLOSING, RunStatus.BLOCKED) or run.result is not None:
        return ControlRefusedError(
            control, ("run", "cleanup"), "cleanup must finish before pause or resume"
        )
    if control.when == "capacity_returns" and run.status != RunStatus.PAUSED:
        return ControlRefusedError(
            control, ("control", "when"), "only a paused run waits for capacity"
        )
    return None
