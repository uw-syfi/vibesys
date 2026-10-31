"""Interrupt a run, resume it, and say whether it ever got stuck.

A run is interrupted by killing its host or by the operator pausing it (`Interruption`). What
follows is what an operator does: Resume, a new host over what the old one left, again if that
host does not end the run, up to `MAX_RESUMES` times. Whatever the last host left is the
outcome of the interruption.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from vs_core.api import (
    ControlId,
    ControlInput,
    PauseOwner,
    RecoveryPhase,
    RequestPhase,
    RunControlEvent,
)
from vs_dst._host import has_ended
from vs_dst._interruptions import Action, Interruption, Moment
from vs_dst._snapshots import RecordingRecords

if TYPE_CHECKING:
    from vs_core.api import CoreState, RunResultProposal
    from vs_dst._effects import Effects
    from vs_dst._host import Hosted, HostPlan
    from vs_dst._snapshots import OutsideWorld, RunningHost, Store
    from vs_dst._world import World
    from vs_proto.api import Record

MAX_RESUMES = 2
"""How many times the operator resumes a run that did not end before giving up on it."""

PATIENCE_FLOOR = 300.0
"""The least a resumed host is waited on, in virtual seconds."""
PATIENCE_FACTOR = 2.0
"""How many times the uninterrupted run's own duration a resumed host is waited on."""

_OPEN = (RequestPhase.DISPATCHED, RequestPhase.RECONCILING, RequestPhase.BLOCKED)
_UNPROVEN = ("pending", "unknown", "blocked")
"""The resolutions of a recovery check that leave its target's fate unproven."""
_DETAIL_LIMIT = 160
_HOW_LIMIT = 48


@dataclass(frozen=True)
class Expectation:
    """What an interrupted run is measured against: the uninterrupted run."""

    result: RunResultProposal | None
    """How it ended."""
    duration: float
    """How long it took, in virtual seconds."""

    @property
    def patience(self) -> float:
        """How long a resumed host is waited on before the operator gives up on it.

        A run that is still neither ended nor parked by then waits on something that is not
        coming (a resume has no more to do than the run itself did).
        """
        return max(PATIENCE_FLOOR, PATIENCE_FACTOR * self.duration)


class Outcome(StrEnum):
    """How a run that was interrupted and resumed ended up."""

    CONTINUED = "continued"
    """It ended as the uninterrupted run did."""
    ENDED_WITH_REASON = "ended with reason"
    """It ended another way, and its result names why."""
    STUCK = "stuck"
    """It did not end after the resumes, or ended with recovery blocked."""
    ERRORED = "errored"
    """The host that was meant to carry on failed with an exception."""


@dataclass(frozen=True)
class Interrupted:
    """A run after an interruption and the resumes that followed it."""

    interruption: Interruption
    outcome: Outcome
    detail: str
    """What ended the run, or what it is stuck on: the result's reason, the error's text, the
    requests it holds open."""
    resumes: int
    """How many hosts the operator started after the interruption."""
    history: tuple[str, ...]
    """How each host that ran after the interruption ended, in a few words (a paused host
    first), such as ``parked``, ``terminal`` or the error that stopped it."""
    core: CoreState | None
    """Core's state as the last host left it."""
    effects: Effects
    expected: RunResultProposal | None = None
    """How the uninterrupted run ended."""
    world: World | None = None
    """The world as the hosts of the interrupted run left it: what it handed out."""
    log_records: tuple[Record, ...] = ()
    """The run.log the hosts left, in order (a step-loop run; none for a shell run)."""

    @property
    def result(self) -> RunResultProposal | None:
        """The run's result, once it has one."""
        return None if self.core is None else self.core.run.result

    @property
    def reruns(self) -> int:
        """How many times an effect began again for a request whose effect had begun before."""
        return len(self.effects.executed) - len(set(self.effects.executed))

    def never_stuck(self) -> bool:
        """Whether the run continued to its end or ended with a named reason."""
        return self.outcome in (Outcome.CONTINUED, Outcome.ENDED_WITH_REASON)

    def matches_uninterrupted(self) -> bool:
        """Whether the run ended exactly as the uninterrupted one did: the same result, adoption included.

        Attempts are not compared: an interruption legitimately costs a step another attempt.
        """
        return self.outcome is Outcome.CONTINUED and self.result == self.expected

    def every_step_finished_once(self) -> bool:
        """Whether no request's effect finished (sealed a result) more than once."""
        return all(count == 1 for count in self.effects.finished.values())


class Pauser:
    """Pauses the run the way an operator does, when the run reaches one moment."""

    def __init__(self, point: Interruption) -> None:
        self._point = point
        self._host: RunningHost | None = None

    def watch(self, store: Store, host: RunningHost) -> None:
        """Start hearing ``store``'s commits, for the run ``host`` drives."""
        self._host = host
        store.watcher = self

    def reached(self, moment: Moment, index: int) -> None:
        """Submit the operator's pause when the run is at the moment the interruption names."""
        if self._host is None or (moment, index) != (self._point.moment, self._point.index):
            return
        # A pause is its own control: a second one, on a resumed host, must not be taken for the
        # first, which the log still holds.
        name = f"operator-pause-{self._point.moment.name.lower()}-{self._point.index}"
        control = ControlInput(
            control_id=ControlId(root=name),
            action="pause",
            owner=PauseOwner.OPERATOR,
        )
        self._host.submit(RunControlEvent(control=control, now_at=self._host.clock.now()))


@dataclass(frozen=True)
class Operator:
    """What the operator does after a stop: resume the run, and stop it again if asked."""

    plan: HostPlan
    patience: float
    again: Interruption | None = None
    """A pause for the first host that resumes the run. Its index counts over the whole run, so
    it only takes hold if that host gets there."""

    def resume(self, outside: OutsideWorld, clock_at: float, hosts: list[Hosted]) -> list[Hosted]:
        """Resume until a host ends the run or the operator has resumed `MAX_RESUMES` times.

        ``hosts`` holds the host that ended before the first resume: none when it was killed.
        Returns it with the hosts the resumes started.
        """
        resumes = 0
        while not hosts or (resumes < MAX_RESUMES and not hosts[-1].finished):
            # A host that dies before the run's first commit leaves nothing to resume.
            resuming = self.plan.resuming(resumed=outside.store.observe() is not None)
            stop = Pauser(self.again) if self.again is not None and resumes == 0 else None
            hosts.append(resuming.run(outside, clock_at, stop, patience=self.patience))
            clock_at = hosts[-1].ended_at + 1
            resumes += 1
        return hosts


def killed(
    operator: Operator,
    outside: OutsideWorld,
    clock_at: float,
    point: Interruption,
    expected: Expectation,
) -> Interrupted:
    """Resume the run ``outside`` holds after its host died, the resume starting at ``clock_at``."""
    return _interrupted(point, expected, outside, operator.resume(outside, clock_at, []))


def paused(
    operator: Operator,
    pristine: OutsideWorld,
    clock_at: float,
    point: Interruption,
    expected: Expectation,
) -> Interrupted:
    """Run the world from its start, pause it at ``point``, and resume it after the host exits."""
    outside = pristine.copy()
    first = operator.plan.run(outside, clock_at, Pauser(point), patience=operator.patience)
    hosts = operator.resume(outside, first.ended_at + 1, [first])
    return _interrupted(point, expected, outside, hosts)


def _interrupted(
    point: Interruption, expected: Expectation, outside: OutsideWorld, hosts: list[Hosted]
) -> Interrupted:
    outcome, detail = _classify(hosts[-1], expected.result)
    resumes = len(hosts) - (point.action is Action.PAUSE)
    history = tuple(_short(host.how, _HOW_LIMIT) for host in hosts)
    store = outside.store
    records = tuple(store.records) if isinstance(store, RecordingRecords) else ()
    return Interrupted(
        point,
        outcome,
        detail,
        resumes,
        history,
        hosts[-1].core,
        outside.effects,
        expected.result,
        outside.world,
        records,
    )


def _classify(last: Hosted, expected: RunResultProposal | None) -> tuple[Outcome, str]:
    """What the last host left of the run: how it ended, and in words why not if it did not."""
    held = _holding(last.core)
    if isinstance(last.ended, Exception):
        return Outcome.ERRORED, _short(f"{type(last.ended).__name__}: {last.ended}{held}")
    if last.ended.halted is not None:
        return Outcome.STUCK, _short(
            f"{type(last.ended.halted).__name__}: {last.ended.halted}{held}"
        )
    outcome, detail = _ended_as(last.ended.core, expected)
    return outcome, _short(detail + (held if outcome is Outcome.STUCK else ""))


def _ended_as(core: CoreState, expected: RunResultProposal | None) -> tuple[Outcome, str]:
    """How the run stands in ``core``, whose host returned: ended, or stuck for a reason."""
    if not has_ended(core):
        recovery = core.records.recovery.phase.value
        return Outcome.STUCK, f"run {core.run.status.value}, recovery {recovery}"
    if core.records.recovery.phase == RecoveryPhase.BLOCKED:
        return Outcome.STUCK, "recovery blocked"
    result = core.run.result
    if result is None:
        return Outcome.STUCK, "terminal with no result"
    if expected is not None and result.outcome == expected.outcome:
        return Outcome.CONTINUED, result.reason
    return Outcome.ENDED_WITH_REASON, f"{result.outcome}: {result.reason}"


def _holding(core: CoreState | None) -> str:
    """The kinds of request the run holds open or cannot account for, as a suffix of a signature."""
    if core is None:
        return ""
    unproven = {
        check.target.root for check in core.records.recovery.checks if check.resolution in _UNPROVEN
    }
    kinds = sorted(
        {
            r.request.kind
            for r in core.records.records
            if r.phase in _OPEN or r.request_id.root in unproven
        }
    )
    return f" [open: {', '.join(kinds)}]" if kinds else ""


def _short(text: str, limit: int = _DETAIL_LIMIT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
