"""Operator steering as durable session input.

A steer control is the operator's free text, recorded by core as a ``steer`` control that
carries only the digest of the text and whom it is for. ``steer_target`` resolves the
operator's target (the run, a role, an attempt or an item, by id) against core
state into core's input target. This module turns each such control into one
``SessionInputReceived`` for that target, which core reserves for the next matching turn,
delivers when that turn is accepted, and records as a receipt. Core's input
records are the one durable account of who has been told what, so a steer reaches a turn
exactly once: a crash between the control and its input is repaired by the next poll,
which derives the missing input from the control itself, and a replay finds the input
already recorded.

The turn message is rendered by the session resolver from the input's artifact; nothing
here builds prompt text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_core.api import (
    AttemptPhase,
    CoreState,
    InputDropped,
    InputId,
    InvocationInputTarget,
    ItemInputTarget,
    RoleId,
    RoleInputTarget,
    Scope,
    ScopeInputTarget,
    SessionInput,
    SessionInputReceived,
)
from vs_runtime._operator_control import RefusalReason, SteerTargetKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import ControlInput, InputTarget
    from vs_runtime._operator_control import SteerTarget


@dataclass(frozen=True)
class TargetRefusal:
    """Why the run cannot resolve a steer's target: no turn could take a message for it."""

    reason: str
    kind: RefusalReason


_ENDED_ATTEMPT = frozenset({AttemptPhase.CLOSING, AttemptPhase.TERMINAL})


def _role(core: CoreState, role: str) -> InputTarget | TargetRefusal:
    declared = core.run.declaration.roles
    if RoleId(root=role) not in declared:
        names = ", ".join(row.root for row in declared) or "none"
        return TargetRefusal(
            f"role {role!r} is not declared by the search (declared: {names})",
            RefusalReason.UNKNOWN_TARGET,
        )
    return RoleInputTarget(role_id=RoleId(root=role))


def _attempt(core: CoreState, attempt: str) -> InputTarget | TargetRefusal:
    rows = [row for row in core.attempts.attempts if row.attempt_id.root == attempt]
    if not rows:
        return TargetRefusal(f"the run holds no attempt {attempt!r}", RefusalReason.UNKNOWN_TARGET)
    latest = max(rows, key=lambda row: row.generation)
    if latest.phase in _ENDED_ATTEMPT:
        return TargetRefusal(f"attempt {attempt!r} has ended", RefusalReason.ENDED_TARGET)
    return ScopeInputTarget(scope=Scope(owner=latest.attempt_id, generation=latest.generation))


def _item(core: CoreState, item: str) -> InputTarget | TargetRefusal:
    rows = [row for row in core.attempts.attempts if row.item_id.root == item]
    if not rows:
        return TargetRefusal(f"the run holds no item {item!r}", RefusalReason.UNKNOWN_TARGET)
    return ItemInputTarget(item_id=rows[0].item_id)


_RESOLVERS: dict[SteerTargetKind, Callable[[CoreState, str], InputTarget | TargetRefusal]] = {
    SteerTargetKind.ROLE: _role,
    SteerTargetKind.ATTEMPT: _attempt,
    SteerTargetKind.ITEM: _item,
}


def steer_target(core: CoreState, target: SteerTarget) -> InputTarget | TargetRefusal | None:
    """Core's input target for ``target``, None for the whole run, or why there is none.

    A role the search does not declare is refused, as is an attempt or item core does not
    hold or an attempt that has ended, since no turn could take a message for it now.
    """
    if target.id is None:
        return None
    return _RESOLVERS[target.kind](core, target.id)


def target_refusal(core: CoreState, target: SteerTarget) -> RefusalReason | None:
    """Why no later state of ``core`` could take a steer to ``target``, or None.

    ``core`` may be a poll behind the run, so only what cannot change is refused: a role the
    search does not declare, or an attempt that has ended. An attempt or item core does not
    hold yet may be created before the run takes the steer; the take resolves it again
    against the state then, and reports it dropped if it still cannot.
    """
    resolved = steer_target(core, target)
    if not isinstance(resolved, TargetRefusal):
        return None
    if target.kind is SteerTargetKind.ROLE or resolved.kind is RefusalReason.ENDED_TARGET:
        return resolved.kind
    return None


def names_target(target: SteerTarget, held: InputTarget | None) -> bool:
    """Whether ``held``, core's input target of a recorded steer, is the one ``target`` names.

    The comparison is by id, so it holds after an attempt is reopened at a later generation.
    """
    match held:
        case None:
            named = (SteerTargetKind.RUN, None)
        case RoleInputTarget(role_id=role):
            named = (SteerTargetKind.ROLE, role.root)
        case ScopeInputTarget(scope=scope):
            named = (SteerTargetKind.ATTEMPT, scope.owner.root)
        case ItemInputTarget(item_id=item):
            named = (SteerTargetKind.ITEM, item.root)
        case InvocationInputTarget():
            return False  # an operator's steer never names one invocation
    return (target.kind, target.id) == named


def steer_input_id(control: ControlInput) -> InputId:
    """The input occurrence one steer control becomes; the control's identity names it."""
    return InputId(root=f"steer-{control.control_id.root}")


def steer_input(
    core: CoreState, control: ControlInput, now_at: float, position: int
) -> SessionInputReceived:
    """The next-turn input for ``control``, which must be a steer with its text.

    The input is for the control's target, or the whole run when it names none.

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
            target=control.target
            or ScopeInputTarget(scope=Scope(owner=core.run.run_id, generation=core.run.generation)),
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


def _sent_as(input_id: InputId) -> str:
    """The id the operator sent a steer under, from its input's id (``steer-op-<id>``).

    A steer sent without an id (a ``control-<n>`` control) is named by that control id.
    """
    control = input_id.root.removeprefix("steer-")
    return control.removeprefix("op-")


class SteerConsumption:
    """Reports each steer once, when core records that a turn received it or dropped it."""

    def __init__(self) -> None:
        """Start with nothing reported; the first poll seeds what already happened."""
        self._reported: set[InputId] | None = None

    def newly_settled(
        self,
        core: CoreState,
        delivered: Callable[[str, str, str], None],
        dropped: Callable[[str], None],
    ) -> None:
        """Report each steer settled since last time.

        ``delivered(role, scope owner, invocation)`` for one a turn received, and
        ``dropped(reason)`` for one core dropped because its target can no longer take input.
        Receipts recorded before the first call are not reported: a resumed run must not
        announce again what its previous process already announced.
        """
        settled = {
            row.input.input_id: row.receipt
            for row in core.sessions.inputs
            if row.receipt is not None and row.input.input_id.root.startswith("steer-")
        }
        if self._reported is None:
            self._reported = set(settled)
            return
        for input_id, receipt in settled.items():
            if input_id in self._reported:
                continue
            self._reported.add(input_id)
            if isinstance(receipt, InputDropped):
                dropped(f"steer {_sent_as(input_id)} reached no turn: {receipt.reason.value}")
                continue
            invocation = next(
                row for row in core.sessions.invocations if row.invocation == receipt.invocation
            )
            delivered(
                invocation.turn.session.role_id.root,
                invocation.scope.owner.root,
                receipt.invocation.invocation_id.root,
            )
