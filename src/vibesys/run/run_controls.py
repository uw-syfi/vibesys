"""The operator's side of one run: what the loop reads of the operator's controls and steers.

The loop polls one object for everything the operator and the tools around the run ask of core:
the control bridge (pause, stop, resume, steer, the control inbox) and the steering a running turn
is offered mid-turn. Both answer with core events, and both are told what core committed and
refused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import ControlRefusedError
from vs_runtime.api.core import MidTurnSteering, RunControlBridge

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_core.api import ArtifactRef, CoreEvent, CoreState
    from vs_runtime.api import ArtifactStore
    from vs_runtime.api.core import TurnSteering
    from vs_runtime.api.step_runner import RunLogStepLog


class StoredSteerArtifacts:
    """Where steer text lives: the run's content-addressed artifact store.

    A steer control carries only the digest of its text, so the text must be readable by
    that digest before the control is submitted.
    """

    def __init__(self, artifacts: ArtifactStore) -> None:
        """Store steer text in the run's content-addressed artifacts."""
        self._artifacts = artifacts

    def put(self, ref: ArtifactRef, text: str) -> None:
        """Store the text; its content address must be the digest the control carries."""
        receipt = self._artifacts.write(text.encode())
        if receipt.sha256 != ref.digest:
            message = f"steer artifact {ref.artifact_id.root!r} stored under {receipt.sha256}"
            raise RuntimeError(message)


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
