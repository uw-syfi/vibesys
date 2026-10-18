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
    InputDropReason,
    InputId,
    InputMode,
    InvocationInputTarget,
    ItemInputTarget,
    RoleId,
    RoleInputTarget,
    Scope,
    ScopeInputTarget,
    SessionInput,
    SessionInputReceived,
)
from vs_runtime._operator_control import (
    RefusalReason,
    SteerMode,
    SteerRequest,
    SteerTargetKind,
    WithdrawRequest,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import ControlInput, InputTarget
    from vs_runtime._operator_control import ControlRequest, SteerTarget


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


def standing_input_id(control_id: str) -> InputId:
    """The input of the steer an operator sent under ``control_id``."""
    return InputId(root=f"steer-op-{control_id}")


def standing_in_force(core: CoreState, control_id: str) -> bool:
    """Whether the steer sent under ``control_id`` is standing guidance core holds in force."""
    input_id = standing_input_id(control_id)
    return any(
        row.input.input_id == input_id
        and row.input.mode is InputMode.STANDING
        and row.receipt is None
        for row in core.sessions.inputs
    )


def _never_standing(core: CoreState, control_id: str) -> bool:
    """Whether no later state could hold the steer sent under ``control_id`` in force.

    So when core holds a control under that id that is not a steer, or the steer's input
    that is not standing guidance or has ended. An id core does not hold yet may be a standing
    steer the run has not taken; the run checks it again when it takes the withdrawal.
    """
    held = {control.control_id.root: control for control in core.run.controls}
    control = held.get(f"op-{control_id}")
    if control is not None and control.action != "steer":
        return True
    input_id = standing_input_id(control_id)
    return any(
        row.input.input_id == input_id
        and (row.input.mode is not InputMode.STANDING or row.receipt is not None)
        for row in core.sessions.inputs
    )


def request_refusal(core: CoreState, request: ControlRequest) -> RefusalReason | None:
    """Why no later state of ``core`` could accept ``request``, or None.

    ``core`` may be a poll behind the run and a refusal is the id's answer for good, so a
    steer is refused only for a target no later state could take, and a withdrawal only for
    a steer that is not, or is no longer, standing guidance; nothing else is refused here.
    """
    match request:
        case SteerRequest(target=target):
            return target_refusal(core, target)
        case WithdrawRequest(withdraws=withdraws) if _never_standing(core, withdraws):
            return RefusalReason.NOT_STANDING
        case _:
            return None


def input_mode(mode: SteerMode) -> InputMode | None:
    """The input mode core records for a steer sent in ``mode``; None is the next turn."""
    match mode:
        case SteerMode.NEXT_TURN:
            return None
        case SteerMode.STANDING:
            return InputMode.STANDING
        case SteerMode.INTERRUPT:
            return InputMode.INTERRUPT


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
            mode=control.input_mode or InputMode.NEXT_TURN,
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
    """Reports each steer once: when a turn received it, or when it reaches no turn any more.

    A next-turn steer ends when a turn receives it or core drops it. Standing guidance ends
    when it is withdrawn, or its target or the run ends; guidance to an attempt also stops
    reaching it when the attempt is reopened as a new generation, which core does not record
    as an end, so that is reported here once too.
    """

    def __init__(self) -> None:
        """Start with nothing reported; the first poll seeds what already happened."""
        self._reported: set[InputId] | None = None

    def newly_settled(
        self,
        core: CoreState,
        delivered: Callable[[str, str, str, str], None],
        delivered_mid_turn: Callable[[str, str, str], None],
        dropped: Callable[[str], None],
        ended: Callable[[str], None],
    ) -> None:
        """Report each steer settled since last time.

        ``delivered(role, scope owner, invocation, note)`` for one a turn received in its
        prompt, where ``note`` says why an interrupt-mode steer reached the next turn instead
        of a running one (empty for any other steer),
        ``delivered_mid_turn(role, scope owner, invocation)`` for one a turn took while it ran,
        ``dropped(reason)`` for one core dropped because its target can no longer take input,
        and ``ended(reason)`` for standing guidance that no longer reaches any turn.
        Receipts recorded before the first call are not reported: a resumed run must not
        announce again what its previous process already announced.
        """
        settled = {
            row.input.input_id: row
            for row in core.sessions.inputs
            if row.input.input_id.root.startswith("steer-")
            and (row.receipt is not None or _superseded(core, row.input))
        }
        if self._reported is None:
            self._reported = set(settled)
            return
        for input_id, row in settled.items():
            if input_id in self._reported:
                continue
            self._reported.add(input_id)
            receipt = row.receipt
            if row.input.mode is InputMode.STANDING:
                why = (
                    receipt.reason.value
                    if isinstance(receipt, InputDropped)
                    else "its attempt was reopened"
                )
                ended(f"standing guidance {_sent_as(input_id)} ended: {why}")
            elif isinstance(receipt, InputDropped):
                dropped(_drop_report(_sent_as(input_id), receipt))
            elif receipt is not None:
                invocation = next(
                    row for row in core.sessions.invocations if row.invocation == receipt.invocation
                )
                where = (
                    invocation.turn.session.role_id.root,
                    invocation.scope.owner.root,
                    receipt.invocation.invocation_id.root,
                )
                if receipt.mid_turn:
                    delivered_mid_turn(*where)
                elif row.input.mode is InputMode.INTERRUPT:
                    delivered(
                        *where,
                        f"interrupt steer {_sent_as(input_id)} reached the next turn: "
                        "no running turn kept it",
                    )
                else:
                    delivered(*where, "")


def _drop_report(name: str, receipt: InputDropped) -> str:
    """What the operator reads about a steer core dropped, and why."""
    if receipt.reason is InputDropReason.DELIVERY_UNKNOWN:
        return (
            f"steer {name} may have reached the agent mid-turn: its turn's provider took it, "
            "and no later turn can confirm it"
        )
    return f"steer {name} reached no turn: {receipt.reason.value}"


def _superseded(core: CoreState, item: SessionInput) -> bool:
    """Whether standing guidance for one attempt generation outlived it: the attempt reopened.

    An input to an attempt names its generation, so a reopened attempt's turns never match it.
    """
    target = item.target
    if item.mode is not InputMode.STANDING or not isinstance(target, ScopeInputTarget):
        return False
    return any(
        row.attempt_id == target.scope.owner and row.generation > target.scope.generation
        for row in core.attempts.attempts
    )
