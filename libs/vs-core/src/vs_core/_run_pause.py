"""Who holds a paused run, and which controls core takes in the run's state.

A run's pause is a fact of its recorded controls, so core keeps no second copy of it:
``run_pause`` folds the controls in order. An operator pause holds the run and drops a
capacity wait the operator asked for earlier. A quota stop pauses a run that is not paused
yet and leaves any other pause as it is. A capacity wait marks the pause it is asked
under, and any immediate resume ends the pause.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from ._operator_actions import ACTION_REFUSAL_TEXT, action_refusal_kind
from ._registry import ContractError
from .types.common import OPERATOR_ACTIONS, PauseOwner, RunPause, RunStatus
from .types.strategy import Finish

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ._operator_actions import ActionRefusal
    from .types.common import ControlInput
    from .types.kernel import CoreState, RunState
    from .types.settlement import RunResultProposal


DEADLINE_STOP: Final = "deadline"
"""The identity of the stop the host delivers when the run's deadline passes.

An operator's stop is a pause (``reopens_on_resume``); the deadline's is the run's time budget
spent, and it ends the run for good.
"""

ENVIRONMENT_STOP: Final = "run-environment"
"""The identity of the stop the host delivers when the run environment can run none of the run's
work (a local Slurm root its probe could not prove shared).

Like the deadline's, it is the host's verdict and not a pause: a resume would meet the same
environment, so it ends the run failed, for good. No operator control can carry it: the host
records operators' controls under ``op-`` and ``control-`` identities.
"""

FINAL_STOPS: Final = frozenset({DEADLINE_STOP, ENVIRONMENT_STOP})
"""The host's own stops: once one stands, no resume reopens the run (``reopens_after``)."""


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
    """Why core refuses a new ``control`` in ``state`` as it stands, or None if it takes it.

    The one statement of the rule: core raises the error when the control arrives, and a
    shell that must answer an operator before core commits asks the same question. A
    terminal run refuses every control, except the resume that reopens a run the operator's
    stop ended (`reopens_on_resume`). Any other run takes a stop, a steer and a
    withdrawal; it takes an operator action that can change something
    (``action_refusal_kind``), also while it stops or cleans up; and a run that recorded a
    stop takes no pause, and a resume only until its ending commits: that resume lifts the stop
    (`stop_standing`).
    """
    run = state.run
    if run.status == RunStatus.TERMINAL:
        lifts = control.action == "resume" and control.when != "capacity_returns"
        if lifts and reopens_on_resume(run):
            return None
        return ControlRefusedError(control, ("run", "status"), "terminal run rejects controls")
    if control.action in OPERATOR_ACTIONS:
        return _action_refusal(state, control)
    if control.action in ("stop", "steer", "withdraw"):
        return None
    return _pause_refusal(run, control)


def _action_refusal(state: CoreState, control: ControlInput) -> ControlRefusedError | None:
    kind = action_refusal_kind(state, control)
    if kind is None:
        return None
    field = {"cancel_attempt": "attempt", "cancel_item": "item"}.get(control.action, "max_parallel")
    return ControlRefusedError(
        control, ("control", field), ACTION_REFUSAL_TEXT[kind], action_refusal=kind
    )


def stop_standing(run: RunState) -> bool:
    """Whether a stop the run recorded still stands: no resume has lifted it since.

    A stop is a pause the operator can lift: an explicit resume takes the stop back
    (`control_refusal`). While the ending's cleanup runs (the run is closing) nothing lifts
    it; once it is terminal a resume reopens the run (`reopens_on_resume`).
    """
    standing = False
    for recorded in run.controls:
        if recorded.action == "stop":
            standing = True
        elif recorded.action == "resume":
            standing = False
    return standing


def reopens_on_resume(run: RunState) -> bool:
    """Whether a resume reopens the run: the operator's stop ended it, and a stop is a pause.

    A stop ends a run through the search's ending, which cancels what is in flight, adopts the
    best and commits a ``cancelled`` result. That ending is the stop's cleanup, not a verdict on
    the run, so the run stays resumable: the explicit resume that opens it takes the stop back
    and reopens the run (`reopened`), which goes on from what it holds. Every other ending is
    final: a result of the run itself, a refused or blocked run, and the deadline's stop (the
    time the run was given is spent), and the run environment's stop (it can run nothing).
    """
    return reopens_after(run.status, run.result, run.controls)


def reopens_after(
    status: RunStatus, result: RunResultProposal | None, controls: Sequence[ControlInput]
) -> bool:
    """`reopens_on_resume` from the three facts it reads: a reader of a record needs no more."""
    if status != RunStatus.TERMINAL or result is None or result.outcome != "cancelled":
        return False
    standing = False
    final = False
    for recorded in controls:
        if recorded.action == "stop":
            standing = True
            final = final or recorded.control_id.root in FINAL_STOPS
        elif recorded.action == "resume":
            standing = final = False
    return standing and not final


def reopened(run: RunState) -> RunState:
    """The run a resume reopened (`reopens_on_resume`): running, with no ending committed.

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
    return run.model_copy(update={"status": RunStatus.RUNNING, "result": None, "receipts": kept})


def _pause_refusal(run: RunState, control: ControlInput) -> ControlRefusedError | None:
    lifts = control.action == "resume" and control.when != "capacity_returns"
    if stop_standing(run) and not lifts:
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
