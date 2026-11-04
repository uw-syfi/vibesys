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

from pydantic import BaseModel, ConfigDict

from vs_core.api import (
    AttemptPhase,
    AttemptView,
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
from vs_runtime._action_controls import submit_refusal
from vs_runtime._operator_control import (
    CancelAttemptRequest,
    CancelItemRequest,
    RefusalReason,
    SetMaxParallelRequest,
    SteerMode,
    SteerRequest,
    SteerTargetKind,
    WithdrawRequest,
)
from vs_runtime._run_control import SteerTurn

if TYPE_CHECKING:
    from vs_core.api import ControlInput, InputTarget
    from vs_runtime._operator_control import ControlRequest, SteerTarget
    from vs_runtime._run_control import RunControlChannel


@dataclass(frozen=True)
class TargetRefusal:
    """Why the run cannot resolve a steer's target: no turn could take a message for it."""

    reason: str
    kind: RefusalReason


_ENDED_ATTEMPT = frozenset({AttemptPhase.CLOSING, AttemptPhase.TERMINAL})


class SteerableAttempt(BaseModel):
    """One attempt of the run, as a steer's target sees it: its item, and whether it ended."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str
    item_id: str
    ended: bool
    """Whether its latest generation is closing or terminal: no turn could take a steer."""


class SteerTargets(BaseModel):
    """What a steer can name in a run, read from core's state: the one rule for a valid target.

    ``roles`` are the session role ids the search declared (the ids a steer's ``role`` target
    and a session's turns use; they are not the plugin's agent ids). The run resolves every
    steer against this rule, and a read-only reader of the run (the chat agent proposing a
    steer) asks the same model, so a proposal the run would refuse is refused first.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    roles: tuple[str, ...]
    attempts: tuple[SteerableAttempt, ...]
    items: tuple[str, ...]

    def ids(self, kind: SteerTargetKind) -> tuple[str, ...]:
        """Every id a target of ``kind`` may name now (an ended attempt is not among them)."""
        match kind:
            case SteerTargetKind.RUN:
                return ()
            case SteerTargetKind.ROLE:
                return self.roles
            case SteerTargetKind.ATTEMPT:
                return tuple(row.attempt_id for row in self.attempts if not row.ended)
            case SteerTargetKind.ITEM:
                return self.items

    def refusal(self, target: SteerTarget) -> TargetRefusal | None:
        """Why no turn could take a steer to ``target`` now, or None when one could."""
        if target.id is None:
            return None  # the whole run: a target model names an id for every other kind
        check = {
            SteerTargetKind.ROLE: self._role_refusal,
            SteerTargetKind.ATTEMPT: self._attempt_refusal,
            SteerTargetKind.ITEM: self._item_refusal,
        }
        return check[target.kind](target.id)

    def _role_refusal(self, name: str) -> TargetRefusal | None:
        if name in self.roles:
            return None
        return TargetRefusal(
            f"role {name!r} is not declared by the search (declared: {_named(self.roles)})",
            RefusalReason.UNKNOWN_TARGET,
        )

    def _attempt_refusal(self, name: str) -> TargetRefusal | None:
        row = next((row for row in self.attempts if row.attempt_id == name), None)
        if row is None:
            return TargetRefusal(
                f"the run holds no attempt {name!r} (open attempts: "
                f"{_named(self.ids(SteerTargetKind.ATTEMPT))})",
                RefusalReason.UNKNOWN_TARGET,
            )
        if row.ended:
            return TargetRefusal(
                f"attempt {name!r} has ended (open attempts: "
                f"{_named(self.ids(SteerTargetKind.ATTEMPT))})",
                RefusalReason.ENDED_TARGET,
            )
        return None

    def _item_refusal(self, name: str) -> TargetRefusal | None:
        if name in self.items:
            return None
        return TargetRefusal(
            f"the run holds no item {name!r} (items: {_named(self.items)})",
            RefusalReason.UNKNOWN_TARGET,
        )


_NAMED_LIMIT = 12


def _named(ids: tuple[str, ...]) -> str:
    """The ids a refusal offers: all of them, or the first few and how many more there are."""
    if not ids:
        return "none"
    shown = ", ".join(ids[:_NAMED_LIMIT])
    return shown if len(ids) <= _NAMED_LIMIT else f"{shown}, and {len(ids) - _NAMED_LIMIT} more"


def known_steer_targets(core: CoreState) -> SteerTargets:
    """The targets a steer may name in ``core``: its declared roles, attempts and items."""
    latest: dict[str, AttemptView] = {}
    for row in core.attempts.attempts:
        held = latest.get(row.attempt_id.root)
        if held is None or row.generation > held.generation:
            latest[row.attempt_id.root] = row
    items = dict.fromkeys(row.item_id.root for row in core.attempts.attempts)
    return SteerTargets(
        roles=tuple(role.root for role in core.run.declaration.roles),
        attempts=tuple(
            SteerableAttempt(
                attempt_id=name, item_id=row.item_id.root, ended=row.phase in _ENDED_ATTEMPT
            )
            for name, row in latest.items()
        ),
        items=tuple(items),
    )


def steer_target(core: CoreState, target: SteerTarget) -> InputTarget | TargetRefusal | None:
    """Core's input target for ``target``, None for the whole run, or why there is none.

    A role the search does not declare is refused, as is an attempt or item core does not
    hold or an attempt that has ended, since no turn could take a message for it now
    (``SteerTargets.refusal``).
    """
    if target.id is None:
        return None
    refusal = known_steer_targets(core).refusal(target)
    if refusal is not None:
        return refusal
    match target.kind:
        case SteerTargetKind.ROLE:
            return RoleInputTarget(role_id=RoleId(root=target.id))
        case SteerTargetKind.ATTEMPT:
            latest = max(
                (row for row in core.attempts.attempts if row.attempt_id.root == target.id),
                key=lambda row: row.generation,
            )
            return ScopeInputTarget(
                scope=Scope(owner=latest.attempt_id, generation=latest.generation)
            )
        case SteerTargetKind.ITEM:
            row = next(row for row in core.attempts.attempts if row.item_id.root == target.id)
            return ItemInputTarget(item_id=row.item_id)
        case SteerTargetKind.RUN:
            return None


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
    a steer that is not, or is no longer, standing guidance, and an action only as
    ``submit_refusal`` says; nothing else is refused here.
    """
    match request:
        case SteerRequest(target=target):
            return target_refusal(core, target)
        case WithdrawRequest(withdraws=withdraws) if _never_standing(core, withdraws):
            return RefusalReason.NOT_STANDING
        case CancelAttemptRequest() | CancelItemRequest() | SetMaxParallelRequest():
            return submit_refusal(core, request)
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


def _operator_id(input_id: InputId) -> str | None:
    """The control id an operator sent a steer under, or None when it was sent without one."""
    prefix = "steer-op-"
    return input_id.root.removeprefix(prefix) if input_id.root.startswith(prefix) else None


class SteerConsumption:
    """Reports each steer once: when a turn received it, or when it reaches no turn any more.

    A next-turn steer ends when a turn receives it or core drops it. Standing guidance is
    reported when its first turn receives it, and ends when it is withdrawn, or its target
    or the run ends; guidance to an attempt also stops reaching it when the attempt is
    reopened as a new generation, which core does not record as an end, so that is reported
    here once too.
    """

    def __init__(self) -> None:
        """Start with nothing reported; the first poll seeds what already happened."""
        self._reported: set[InputId] | None = None
        self._in_force: set[InputId] = set()

    def newly_settled(self, core: CoreState, channel: RunControlChannel) -> None:
        """Report to ``channel`` each steer step that happened since last time.

        A steer a turn received in its prompt (``notify_steer_consumed``, whose ``note`` says
        why an interrupt-mode steer reached the next turn instead of a running one), one a
        turn took while it ran (``notify_steer_delivered_mid_turn``), standing guidance's
        first turn (``notify_guidance_reached``), one core dropped because its target can no
        longer take input (``notify_steer_dropped``), and standing guidance that no longer
        reaches any turn (``notify_guidance_ended``). Every report names the control id the
        operator sent the steer under and, for a turn, the attempt it ran in. Receipts and
        turns recorded before the first call are not reported: a resumed run must not
        announce again what its previous process already announced.
        """
        first = self._reported is None
        self._report_guidance_reached(core, channel, seed=first)
        settled = {
            row.input.input_id: row
            for row in core.sessions.inputs
            if row.input.input_id.root.startswith("steer-")
            and (row.receipt is not None or _superseded(core, row.input))
        }
        if self._reported is None:
            self._reported = set(settled)
            return
        attempts = {row.attempt_id.root for row in core.attempts.attempts}
        for input_id, row in settled.items():
            if input_id in self._reported:
                continue
            self._reported.add(input_id)
            receipt = row.receipt
            control_id = _operator_id(input_id)
            if row.input.mode is InputMode.STANDING:
                why = (
                    receipt.reason.value
                    if isinstance(receipt, InputDropped)
                    else "its attempt was reopened"
                )
                channel.notify_guidance_ended(
                    f"standing guidance {_sent_as(input_id)} ended: {why}",
                    why=why,
                    control_id=control_id,
                )
            elif isinstance(receipt, InputDropped):
                text, why = _drop_report(_sent_as(input_id), receipt)
                channel.notify_steer_dropped(text, why=why, control_id=control_id)
            elif receipt is not None:
                invocation = next(
                    row for row in core.sessions.invocations if row.invocation == receipt.invocation
                )
                owner = invocation.scope.owner.root
                turn = SteerTurn(
                    role=invocation.turn.session.role_id.root,
                    round_label=owner,
                    execution_id=receipt.invocation.invocation_id.root,
                    attempt_id=owner if owner in attempts else None,
                )
                if receipt.mid_turn:
                    channel.notify_steer_delivered_mid_turn(turn, control_id=control_id)
                else:
                    note = (
                        f"interrupt steer {_sent_as(input_id)} reached the next turn: "
                        "no running turn kept it"
                        if row.input.mode is InputMode.INTERRUPT
                        else ""
                    )
                    channel.notify_steer_consumed(turn, control_id=control_id, note=note)

    def _report_guidance_reached(
        self, core: CoreState, channel: RunControlChannel, *, seed: bool
    ) -> None:
        """Report each standing steer's first turn; ``seed`` only records what already did."""
        attempts = {row.attempt_id.root for row in core.attempts.attempts}
        for invocation in core.sessions.invocations:
            for input_id in invocation.guidance_ids:
                if input_id in self._in_force or not input_id.root.startswith("steer-"):
                    continue
                self._in_force.add(input_id)
                if seed:
                    continue
                owner = invocation.scope.owner.root
                turn = SteerTurn(
                    role=invocation.turn.session.role_id.root,
                    round_label=owner,
                    execution_id=invocation.invocation.invocation_id.root,
                    attempt_id=owner if owner in attempts else None,
                )
                channel.notify_guidance_reached(turn, control_id=_operator_id(input_id))


def _drop_report(name: str, receipt: InputDropped) -> tuple[str, str]:
    """What the operator reads about a steer core dropped, and the bare reason why."""
    if receipt.reason is InputDropReason.DELIVERY_UNKNOWN:
        why = "its turn's provider took it, and no later turn can confirm it"
        return f"steer {name} may have reached the agent mid-turn: {why}", why
    return f"steer {name} reached no turn: {receipt.reason.value}", receipt.reason.value


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
