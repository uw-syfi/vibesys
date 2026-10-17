"""Operator steering as durable session input.

A steer control is the operator's free text, recorded by core as a ``steer`` control that
carries only the digest of the text. This module turns each such control into one
``SessionInputReceived`` for the whole run, which core reserves for the next turn of any
session, delivers when that turn is accepted, and records as a receipt. Core's input
records are the one durable account of who has been told what, so a steer reaches a turn
exactly once: a crash between the control and its input is repaired by the next poll,
which derives the missing input from the control itself, and a replay finds the input
already recorded.

The turn message is rendered by the session resolver from the input's artifact; nothing
here builds prompt text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import (
    CoreState,
    InputDelivered,
    InputId,
    Scope,
    ScopeInputTarget,
    SessionInput,
    SessionInputReceived,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import ControlInput


def steer_input_id(control: ControlInput) -> InputId:
    """The input occurrence one steer control becomes; the control's identity names it."""
    return InputId(root=f"steer-{control.control_id.root}")


def steer_input(
    core: CoreState, control: ControlInput, now_at: float, position: int
) -> SessionInputReceived:
    """The run-wide next-turn input for ``control``, which must be a steer with its text.

    ``position`` is the control's 1-based place among the run's controls; it orders the
    steers, whatever their identities.
    """
    if control.action != "steer" or control.artifact is None:
        message = "only a steer control with its text becomes session input"
        raise ValueError(message)
    sequence = position
    return SessionInputReceived(
        input=SessionInput(
            input_id=steer_input_id(control),
            target=ScopeInputTarget(
                scope=Scope(owner=core.run.run_id, generation=core.run.generation)
            ),
            artifact=control.artifact,
            received_at=now_at,
            sequence=sequence,
        )
    )


def unrecorded_steers(core: CoreState, now_at: float) -> tuple[SessionInputReceived, ...]:
    """Inputs for steer controls core holds but whose input it has not recorded.

    Empty in the steady state; non-empty only after a crash between a control and its
    input, or on the poll that follows a resume.
    """
    recorded = {row.input.input_id for row in core.sessions.inputs}
    return tuple(
        steer_input(core, control, now_at, position)
        for position, control in enumerate(core.run.controls, start=1)
        if control.action == "steer"
        and control.artifact is not None
        and steer_input_id(control) not in recorded
    )


class SteerConsumption:
    """Reports each steer once, when core records that a turn received it."""

    def __init__(self) -> None:
        """Start with no delivery reported; the first poll seeds what already happened."""
        self._reported: set[InputId] | None = None

    def newly_delivered(self, core: CoreState, report: Callable[[str, str, str], None]) -> None:
        """Call ``report(role, scope owner, invocation)`` for each steer delivered since last time.

        Deliveries recorded before the first call are not reported: a resumed run must not
        announce again what its previous process already announced.
        """
        delivered = {
            row.input.input_id: row.receipt
            for row in core.sessions.inputs
            if isinstance(row.receipt, InputDelivered)
            and row.input.input_id.root.startswith("steer-")
        }
        if self._reported is None:
            self._reported = set(delivered)
            return
        for input_id, receipt in delivered.items():
            if input_id in self._reported:
                continue
            self._reported.add(input_id)
            invocation = next(
                row for row in core.sessions.invocations if row.invocation == receipt.invocation
            )
            report(
                invocation.turn.session.role_id.root,
                invocation.scope.owner.root,
                receipt.invocation.invocation_id.root,
            )
