"""The operator's side of one run: what the loop reads of the operator's controls and steers.

The loop polls one object for everything the operator and the tools around the run ask of core:
the control bridge (pause, stop, resume, steer, the control inbox) and the steering a running turn
is offered mid-turn. Both answer with core events, and both are told what core committed and
refused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import ControlRefusedError
from vs_runtime._mid_turn_steering import MidTurnSteering

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_core.api import CoreEvent, CoreState
    from vs_runtime._control_bridge import RunControlBridge
    from vs_runtime._mid_turn_steering import TurnSteering
    from vs_runtime._run_log_step_log import RunLogStepLog


class OperatorControls:
    """The operator's controls and the steers offered to running turns, as one poll."""

    def __init__(self, bridge: RunControlBridge, steering: TurnSteering | None) -> None:
        """Poll ``bridge``; offer an interrupt-mode input to a running turn through ``steering``."""
        self._bridge = bridge
        self._steering = None if steering is None else MidTurnSteering(steering)

    def poll(self, core: CoreState, now_at: float) -> tuple[CoreEvent, ...]:
        """The controls requested since the last poll, then the steers a running turn is offered."""
        events = list(self._bridge.poll(core, now_at))
        if self._steering is not None:
            events.extend(self._steering.poll(core, now_at))
        return tuple(events)

    def settled(
        self, core: CoreState, *, running: bool, refused: Sequence[ControlRefusedError] = ()
    ) -> None:
        """Tell the operator what core committed and which of its controls it refused."""
        self._bridge.settled(core, running=running, refused=refused)

    def end(self, core: CoreState) -> None:
        """Answer every control left once the run ended; a run that may resume keeps its requests."""
        self._bridge.end(core)

    def bring_in(self, log: RunLogStepLog, now_at: float) -> None:
        """Append what the operator asked of a run before the loop looks at it.

        The loop returns at once for a run at rest, so a relaunch that resumes a parked or stopped
        run must have its controls in the log first.
        """
        refused: list[ControlRefusedError] = []
        for event in self.poll(log.state, now_at):
            try:
                log.input(event)
            except ControlRefusedError as refusal:
                refused.append(refusal)
        self.settled(log.state, running=False, refused=refused)
