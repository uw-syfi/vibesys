"""Turn permits and operator controls over a native replay prefix, without CoreState.

A permit is the admission of one logical turn: the epoch whose physical start
admitted it, the turn's logical origin and its own recorded bound. Requests the
turn makes carry it, so they cannot outlive the turn and never inherit another
turn's deadline. A control is classified against the committed prefix before its
inbox entry is settled or rejected; the archive classifies it again on reload, so
a stale control can never be recorded as applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from protobuf import Oneof

from vs_proto.api import replay as pb

from ._replay_sources import ReplayError


class PermitRefusal(StrEnum):
    """Why a request's permit is not its turn's current admission."""

    STALE_EPOCH = "stale_epoch"
    OTHER_TURN = "other_turn"
    OTHER_BOUND = "other_bound"


def permit_refusal(
    permit: pb.ReplayTurnPermit, admitted: pb.ReplayTurnPermit, *, epoch: int
) -> PermitRefusal | None:
    """Whether ``permit`` is ``admitted``, the requesting turn's permit in the open ``epoch``."""
    if permit.epoch != epoch or admitted.epoch != epoch:
        return PermitRefusal.STALE_EPOCH
    if permit.turn != admitted.turn:
        return PermitRefusal.OTHER_TURN
    if permit.bound != admitted.bound:
        return PermitRefusal.OTHER_BOUND
    return None


@dataclass(frozen=True)
class ControlView:
    """The committed facts a control is classified against.

    ``permits`` are the turns admitted in the open epoch, in start order;
    ``unfinished`` are physical children neither ended nor withdrawn; ``live_turns``
    are the logical turns that have not ended.
    """

    epoch: int
    open: bool
    permits: tuple[pb.ReplayTurnPermit, ...]
    unfinished: tuple[str, ...]
    live_turns: frozenset[str]


def classify_control(control: pb.ReplayControl, view: ControlView) -> pb.ReplayControlDisposition:
    """The one disposition ``control`` has against ``view``."""
    if control.epoch != view.epoch or not view.open:
        return _stale(control, pb.ReplayControlStale.EPOCH)
    match control.action:
        case Oneof("pause", _):
            applied = pb.ReplayControlApplied(draining=list(view.permits))
        case Oneof("pause_now", _):
            applied = pb.ReplayControlApplied(withdrawn=list(view.unfinished))
        case Oneof("steer", steer) if isinstance(steer, pb.ReplaySteer):
            if steer.has_field("turn") and steer.turn not in view.live_turns:
                return _stale(control, pb.ReplayControlStale.TURN)
            applied = pb.ReplayControlApplied()
        case _:
            problem = "a replay control requires an action"
            raise ReplayError(problem)
    return pb.ReplayControlDisposition(control=control, outcome=Oneof("applied", applied))


def applied_control(disposition: pb.ReplayControlDisposition) -> pb.ReplayControlApplied | None:
    """What an applied disposition did, or None for a stale control."""
    match disposition.outcome:
        case Oneof("applied", applied) if isinstance(applied, pb.ReplayControlApplied):
            return applied
        case _:
            return None


def stale_control(disposition: pb.ReplayControlDisposition) -> pb.ReplayControlStale | None:
    """Why a disposition left its control unapplied, or None for an applied one."""
    match disposition.outcome:
        case Oneof("stale", reason) if isinstance(reason, pb.ReplayControlStale):
            return reason
        case _:
            return None


def _stale(control: pb.ReplayControl, reason: pb.ReplayControlStale) -> pb.ReplayControlDisposition:
    return pb.ReplayControlDisposition(control=control, outcome=Oneof("stale", reason))


@dataclass(frozen=True)
class ReplayControls:
    """Recorded dispositions, the open epoch's Pause drain or pause-now, and pending guidance.

    A fresh epoch inherits no drain and no pause-now. Applied guidance stays
    pending until a turn start it applies to consumes all of it, in order.
    """

    _dispositions: dict[str, bytes] = field(default_factory=dict, repr=False)
    _draining: frozenset[str] | None = None
    _halted: bool = False
    _guidance: tuple[tuple[str, str | None], ...] = ()
    _steered: frozenset[str] = frozenset()

    def opened(self) -> ReplayControls:
        """The controls a fresh epoch starts with."""
        return ReplayControls(
            self._dispositions,
            _draining=None,
            _halted=False,
            _guidance=self._guidance,
            _steered=self._steered,
        )

    def disposition(self, request_id: str) -> pb.ReplayControlDisposition | None:
        value = self._dispositions.get(request_id)
        return None if value is None else pb.ReplayControlDisposition.from_binary(value)

    @property
    def halted(self) -> bool:
        """Whether an applied pause-now admits no physical start in the open epoch."""
        return self._halted

    @property
    def draining(self) -> frozenset[str] | None:
        """The logical turns a Pause still admits continuations of, or None without a Pause."""
        return self._draining

    def admits(self, origin: str | None) -> bool:
        """Whether a physical start of logical turn ``origin`` (None: a fresh turn) is admitted."""
        if self._halted:
            return False
        if self._draining is None:
            return True
        return origin is not None and origin in self._draining

    def guidance(self, origin: str) -> tuple[str, ...]:
        """The pending steer input ids a start of ``origin`` consumes, in application order."""
        return tuple(key for key, turn in self._guidance if turn is None or turn == origin)

    def record(self, disposition: pb.ReplayControlDisposition, view: ControlView) -> ReplayControls:
        """Fold a recorded disposition, which must be the one the prefix classifies."""
        control = disposition.control
        if control is None:
            problem = "a control disposition requires its control"
            raise ReplayError(problem)
        if control.request_id in self._dispositions:
            problem = "a control request is disposed of once"
            raise ReplayError(problem)
        if classify_control(control, view).to_binary() != disposition.to_binary():
            problem = "control disposition contradicts the committed prefix"
            raise ReplayError(problem)
        dispositions = {**self._dispositions, control.request_id: disposition.to_binary()}
        draining, halted = self._draining, self._halted
        guidance, steered = self._guidance, self._steered
        applied = applied_control(disposition)
        match control.action if applied is not None else None:
            case Oneof("pause", _) if applied is not None:
                origins = frozenset(permit.turn for permit in applied.draining)
                draining = origins if draining is None else draining & origins
            case Oneof("pause_now", _):
                halted = True
            case Oneof("steer", steer) if isinstance(steer, pb.ReplaySteer):
                if steer.input_id in steered:
                    problem = "one steer input is applied once"
                    raise ReplayError(problem)
                steered |= {steer.input_id}
                turn = steer.turn if steer.has_field("turn") else None
                guidance = (*guidance, (steer.input_id, turn))
            case _:
                pass
        return ReplayControls(dispositions, draining, halted, guidance, steered)

    def start(self, origin: str, consumed: tuple[str, ...]) -> ReplayControls:
        """Fold a physical start of ``origin`` that consumed ``consumed`` guidance."""
        if not self.admits(origin):
            problem = "a Pause admits only continuations of its permits; pause-now admits none"
            raise ReplayError(problem)
        if consumed != self.guidance(origin):
            problem = "a turn start consumes exactly the guidance pending for it"
            raise ReplayError(problem)
        taken = set(consumed)
        guidance = tuple(item for item in self._guidance if item[0] not in taken)
        return ReplayControls(
            self._dispositions, self._draining, self._halted, guidance, self._steered
        )
