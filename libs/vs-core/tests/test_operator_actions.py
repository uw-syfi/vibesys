"""The operator's typed actions on core entities, applied by core alike in every search.

The toy search runs workstreams on core's agent pipeline (``toy_agent_search``). An operator
cancels an attempt or an item, or changes the parallel limit, just before a chosen turn
answers. The tests check what core owns:

- a cancelled attempt closes cancelled through its own lifecycle, the search hears it, and
  no turn of it is dispatched afterwards;
- no attempt starts at a cancelled item;
- admission holds no more attempts than the limit in force allows;
- each action leaves its notice, which the next agent turn receives;
- the run still ends;
- core refuses an action exactly when ``control_refusal`` says it does, also after a stop,
  and a replay of an action changes nothing;
- a cancel of a parked or blocked attempt, and a limit above the launch limit, are refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

import vs_core.api as core

from .test_attempt_retirement import REDUCERS as QUEUED_REOPEN_REDUCERS
from .test_attempt_retirement import reopen_fixture as queued_reopen_fixture
from .test_reopen_kernel_bridge import reopen_fixture
from .toy_agent_search import LIMITS, Config, ToyHost, run_toy, summary

if TYPE_CHECKING:
    # test-isolation: the trace type is test support published at vs_core.testing; vs_core.api carries no test support.
    from vs_core.testing.drive import Trace

NOTICE = core.ArtifactRef(artifact_id=core.ArtifactId(root="notice"), digest="notice")

type _Act = Literal["cancel_attempt", "cancel_item", "set_max_parallel"]


def _action(
    control_id: str,
    action: core.ControlAction,
    *,
    attempt: str | None = None,
    item: str | None = None,
    max_parallel: int | None = None,
) -> core.ControlInput:
    return core.ControlInput(
        control_id=core.ControlId(root=control_id),
        action=action,
        artifact=NOTICE,
        attempt=None if attempt is None else core.AttemptId(root=attempt),
        item=None if item is None else core.ItemId(root=item),
        max_parallel=max_parallel,
    )


@dataclass
class OperatorHost(ToyHost):
    """The toy world, plus an operator who acts just before the n-th turn answers.

    The action is built from the attempt that turn runs in (the toy's item ids are its
    attempt ids). It is sent only if core would take it then (``control_refusal``), so ``sent``
    is what core applied. ``turns`` records the attempt of every turn dispatched, in order,
    and ``slots`` how many attempts admission held as each request was dispatched; ``refused``
    the feedback core gave a start at a cancelled item, asked just after the action.
    """

    at: int = 0
    act: _Act = "cancel_attempt"
    limit: int = 1
    sent: list[core.ControlInput] = field(default_factory=list)
    turns: list[str] = field(default_factory=list)
    sent_after: int | None = None
    slots: list[int] = field(default_factory=list)
    slots_at_change: int | None = None
    change_index: int | None = None
    refused: list[core.StrategyEvent] = field(default_factory=list)

    def during_turn(
        self, request: core.Request, state: core.CoreState
    ) -> tuple[core.CoreEvent, ...]:
        """The operator's action, then whatever the toy world does during the turn."""
        self.slots.append(len(state.scheduling.slots))
        events = super().during_turn(request, state)
        if not isinstance(request, core.DispatchTurn):
            return events
        ordinal = len(self.turns)
        self.turns.append(request.scope.owner.root)
        if ordinal != self.at or self.sent:
            return events
        owner = request.scope.owner.root
        match self.act:
            case "cancel_attempt":
                control = _action("op", "cancel_attempt", attempt=owner)
            case "cancel_item":
                control = _action("op", "cancel_item", item=owner)
            case "set_max_parallel":
                control = _action("op", "set_max_parallel", max_parallel=self.limit)
        if core.control_refusal(state, control) is not None:
            return events
        self.sent.append(control)
        self.sent_after = len(self.turns)
        self.slots_at_change = len(state.scheduling.slots)
        self.change_index = len(self.slots)
        event = core.RunControlEvent(control=control, now_at=state.run.now_at)
        if control.item is not None:
            self.refused.extend(_restart_at(core.step(state, event).state, control.item))
        return (event, *events)


def _restart_at(state: core.CoreState, item: core.ItemId) -> tuple[core.StrategyEvent, ...]:
    """What core answers a search that starts a new attempt at ``item`` in ``state``."""
    start = core.StartAttempt(
        decision_id=core.DecisionId(root="again"),
        scope=core.Scope(owner=state.run.run_id, generation=state.run.generation),
        attempt_id=core.AttemptId(root=f"{item.root}-again"),
        item_id=item,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.ISOLATED_CHILD, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(admission_charge=1, paid_invocation_limit=6),
    )
    submitted = core.DecisionSubmitted(
        decision=core.Wait(request=start, until=core.Within(limit=10.0)),
        expected_revision=state.revision,
    )
    return core.step(state, submitted).events


def _cancelled(control: core.ControlInput) -> str:
    subject = control.attempt or control.item
    assert subject is not None
    return subject.root


def _assert_notice_settled(trace: Trace, control: core.ControlInput) -> None:
    """The notice reached a turn, or core dropped it because no turn was left to take it."""
    row = next(
        row
        for row in trace.core.sessions.inputs
        if row.input.input_id == core.notice_input_id(control)
    )
    assert row.input.target == core.ScopeInputTarget(
        scope=core.Scope(owner=trace.core.run.run_id, generation=trace.core.run.generation)
    )
    assert row.receipt is not None, "the notice is still waiting in an ended run"


@settings(max_examples=40, deadline=None)
@given(
    at=st.integers(min_value=0, max_value=5),
    workstreams=st.integers(min_value=1, max_value=3),
    in_flight=st.integers(min_value=1, max_value=3),
    act=st.sampled_from(["cancel_attempt", "cancel_item"]),
)
@example(at=0, workstreams=1, in_flight=1, act="cancel_attempt")
@example(at=1, workstreams=2, in_flight=2, act="cancel_item")
def test_a_cancel_closes_its_attempt_cancelled_and_the_run_still_ends(
    at: int, workstreams: int, in_flight: int, act: _Act
) -> None:
    host = OperatorHost(at=at, act=act)
    trace = run_toy(host, Config(workstreams=workstreams, in_flight=in_flight))

    assert trace.finished, "the run did not end"
    assert trace.core.run.status == core.RunStatus.TERMINAL
    for control in host.sent:
        name = _cancelled(control)
        settled = [
            event.settlement
            for event in trace.events
            if isinstance(event, core.AttemptSettled)
            and event.settlement.attempt.attempt_id.root == name
        ]
        # The search hears the close (``WorkspaceClosed``) through this one settlement.
        assert len(settled) == 1, settled
        assert settled[0].outcome == "cancelled"
        assert not settled[0].eligible
        assert host.sent_after is not None
        assert name not in host.turns[host.sent_after :], "a cancelled attempt took a turn"
        _assert_notice_settled(trace, control)
    for event in host.refused:
        assert isinstance(event, core.Rejected)
        assert event.path == ("item_id",)
    assert len(host.refused) == sum(1 for c in host.sent if c.action == "cancel_item")


@settings(max_examples=30, deadline=None)
@given(
    at=st.integers(min_value=0, max_value=4),
    limit=st.integers(min_value=1, max_value=3),
    workstreams=st.integers(min_value=2, max_value=4),
)
@example(at=0, limit=1, workstreams=3)
def test_a_lower_parallel_limit_admits_nothing_new_until_attempts_end(
    at: int, limit: int, workstreams: int
) -> None:
    host = OperatorHost(at=at, act="set_max_parallel", limit=limit)
    trace = run_toy(host, Config(workstreams=workstreams, in_flight=workstreams))

    assert trace.finished, "the run did not end"
    assert core.parallel_limit(trace.core.run) == (limit if host.sent else LIMITS.max_parallel)
    if not host.sent:
        return
    assert host.slots_at_change is not None
    assert host.change_index is not None
    # Attempts running when the limit fell keep running, but none is admitted while the run
    # holds as many as the limit: the count only falls until it is within the limit.
    bound = max(limit, host.slots_at_change)
    for held in host.slots[host.change_index :]:
        assert held <= bound
        bound = max(limit, min(bound, held))
    _assert_notice_settled(trace, host.sent[0])


@dataclass
class _LoweredHost(OperatorHost):
    """The operator lowers the limit to 1; ``held`` is the first state after that in which
    the run holds at least one attempt but fewer than its launch limit."""

    held: core.CoreState | None = None

    def during_turn(
        self, request: core.Request, state: core.CoreState
    ) -> tuple[core.CoreEvent, ...]:
        if (
            self.held is None
            and core.parallel_limit(state.run) == 1
            and 1 <= len(state.scheduling.slots) < LIMITS.max_parallel
        ):
            self.held = state
        return super().during_turn(request, state)


def _held_to_one() -> core.CoreState:
    """A run the operator holds to one attempt at a time, holding more than one."""
    host = _LoweredHost(at=0, act="set_max_parallel", limit=1)
    run_toy(host, Config(workstreams=3, in_flight=3))
    assert host.held is not None, "the run never held attempts under the lowered limit"
    return host.held


def _waiting(state: core.CoreState) -> core.CoreState:
    """``state`` with one more attempt the search asked to start, waiting for admission."""
    start = core.StartAttempt(
        decision_id=core.DecisionId(root="extra"),
        scope=core.Scope(owner=state.run.run_id, generation=state.run.generation),
        attempt_id=core.AttemptId(root="extra"),
        item_id=core.ItemId(root="extra"),
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.ISOLATED_CHILD, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(admission_charge=1, paid_invocation_limit=6),
    )
    submitted = core.DecisionSubmitted(
        decision=core.Wait(request=start, until=core.Within(limit=10.0)),
        expected_revision=state.revision,
    )
    return core.step(state, submitted).state


def _admitted(state: core.CoreState) -> bool:
    return any(slot.attempt.attempt_id.root == "extra" for slot in state.scheduling.slots)


def _limited(state: core.CoreState, control_id: str, limit: int) -> core.CoreState:
    control = _action(control_id, "set_max_parallel", max_parallel=limit)
    return core.step(state, core.RunControlEvent(control=control, now_at=state.run.now_at)).state


def test_the_latest_parallel_limit_is_in_force() -> None:
    """Each new limit replaces the one before it, whichever was lower."""
    state = _held_to_one()
    for control_id, limit in (("l2", 2), ("l1", 1), ("l3", 3)):
        state = _limited(state, control_id, limit)
        assert core.parallel_limit(state.run) == limit


def test_the_scheduling_view_offers_no_admission_beyond_the_limit_in_force() -> None:
    """The view a search plans from counts the operator's limit, not the launch limit."""
    state = _held_to_one()

    assert core.project(state).scheduling.available_tokens == 0


def test_admission_starts_nothing_while_the_run_holds_the_limit_in_force() -> None:
    """An attempt asked for while the run is full under the operator's limit waits."""
    state = _waiting(_held_to_one())

    assert not _admitted(state)
    assert not _admitted(_limited(state, "again", 1))


def test_raising_the_limit_admits_a_waiting_attempt_at_once() -> None:
    """A raise admits what now fits in the same step, without waiting for another event."""
    state = _waiting(_held_to_one())

    assert _admitted(_limited(state, "raise", LIMITS.max_parallel))


@pytest.mark.parametrize("limit", [1, LIMITS.max_parallel, LIMITS.max_parallel + 1])
def test_a_parallel_limit_above_the_launch_limit_is_refused(limit: int) -> None:
    """The launch limit sized the run's host: an operator may lower the limit, not raise it."""
    state, _ = _state_with_attempt()
    control = _action("a", "set_max_parallel", max_parallel=limit)
    event = core.RunControlEvent(control=control, now_at=state.run.now_at)
    if limit <= state.run.limits.max_parallel:
        assert core.parallel_limit(core.step(state, event).state.run) == limit
        return
    with pytest.raises(core.ControlRefusedError, match="launched with") as raised:
        core.step(state, event)
    assert raised.value.action_refusal is core.ActionRefusal.ABOVE_LAUNCH_LIMIT


def _state_with_attempt() -> tuple[core.CoreState, str]:
    """A run with one attempt running a turn, as the operator sees it then."""
    seen: list[tuple[core.CoreState, str]] = []

    @dataclass
    class Watcher(ToyHost):
        def during_turn(
            self, request: core.Request, state: core.CoreState
        ) -> tuple[core.CoreEvent, ...]:
            if isinstance(request, core.DispatchTurn) and not seen:
                seen.append((state, request.scope.owner.root))
            return super().during_turn(request, state)

    run_toy(Watcher())
    return seen[0]


@given(
    act=st.sampled_from(["cancel_attempt", "cancel_item", "set_max_parallel"]),
    known=st.booleans(),
    twice=st.booleans(),
)
@example(act="cancel_attempt", known=True, twice=False)
@example(act="cancel_item", known=True, twice=True)
@example(act="cancel_attempt", known=False, twice=False)
@example(act="cancel_item", known=False, twice=False)
@example(act="set_max_parallel", known=False, twice=False)
def test_core_refuses_an_action_exactly_when_control_refusal_does(
    *, act: _Act, known: bool, twice: bool
) -> None:
    state, attempt = _state_with_attempt()
    subject = attempt if known else "nothing"
    control = {
        "cancel_attempt": _action("a", "cancel_attempt", attempt=subject),
        "cancel_item": _action("a", "cancel_item", item=subject),
        "set_max_parallel": _action("a", "set_max_parallel", max_parallel=2),
    }[act]
    refusal = core.control_refusal(state, control)
    assert (refusal is None) == (known or act == "set_max_parallel")
    event = core.RunControlEvent(control=control, now_at=state.run.now_at)
    if refusal is not None:
        with pytest.raises(core.ControlRefusedError) as raised:
            core.step(state, event)
        assert raised.value.path == refusal.path
        return
    after = core.step(state, event).state
    if twice:
        # The same control again is a replay: it changes nothing but the revision every
        # committed event advances, and asks for nothing.
        replayed = core.step(after, event)
        assert replayed.state.model_copy(update={"revision": after.revision}) == after
        assert replayed.requests == ()
        return
    # A second, different cancel of what is already cancelled is refused by the same rule.
    again = control.model_copy(update={"control_id": core.ControlId(root="b")})
    if act == "set_max_parallel":
        assert core.control_refusal(after, again) is None
    else:
        assert core.control_refusal(after, again) is not None


def _stopped(state: core.CoreState) -> core.CoreState:
    """``state`` once the operator asked the run to drain and stop: it is finishing."""
    stop = core.ControlInput(
        control_id=core.ControlId(root="s"), action="stop", mode=core.StopMode.DRAIN
    )
    return core.step(state, core.RunControlEvent(control=stop, now_at=state.run.now_at)).state


@pytest.mark.parametrize("status", [None, core.RunStatus.CLOSING])
@pytest.mark.parametrize("act", ["cancel_attempt", "cancel_item", "set_max_parallel"])
def test_a_stopping_run_takes_the_operators_actions(
    act: _Act, status: core.RunStatus | None
) -> None:
    """Cancelling a straggler while the run drains is what a cancel is for.

    A run that recorded a stop, or is cleaning up, refuses a pause or a resume, but not an
    action that changes something.
    """
    state, attempt = _state_with_attempt()
    state = _stopped(state)
    if status is not None:
        state = state.model_copy(update={"run": state.run.model_copy(update={"status": status})})
    control = {
        "cancel_attempt": _action("a", "cancel_attempt", attempt=attempt),
        "cancel_item": _action("a", "cancel_item", item=attempt),
        "set_max_parallel": _action("a", "set_max_parallel", max_parallel=1),
    }[act]
    pause = core.ControlInput(
        control_id=core.ControlId(root="p"), action="pause", owner=core.PauseOwner.OPERATOR
    )

    assert core.control_refusal(state, pause) is not None
    assert core.control_refusal(state, control) is None
    core.step(state, core.RunControlEvent(control=control, now_at=state.run.now_at))


@pytest.mark.parametrize("phase", [core.AttemptPhase.PARKED, core.AttemptPhase.BLOCKED])
def test_a_cancel_of_a_parked_attempt_is_refused_with_why(phase: core.AttemptPhase) -> None:
    """A parked or blocked attempt runs nothing a cancel could stop; the operator hears so."""
    state, attempt = _state_with_attempt()
    rows = tuple(
        row.model_copy(update={"phase": phase}) if row.attempt_id.root == attempt else row
        for row in state.attempts.attempts
    )
    state = state.model_copy(
        update={"attempts": state.attempts.model_copy(update={"attempts": rows})}
    )
    control = _action("a", "cancel_attempt", attempt=attempt)

    refusal = core.control_refusal(state, control)

    assert refusal is not None
    assert refusal.action_refusal is core.ActionRefusal.PARKED
    assert refusal.detail == core.ACTION_REFUSAL_TEXT[core.ActionRefusal.PARKED]


_FIELDS: dict[str, dict[str, object]] = {
    "attempt": {"attempt": core.AttemptId(root="a")},
    "item": {"item": core.ItemId(root="i")},
    "max_parallel": {"max_parallel": 2},
}
_OWN_FIELD = {
    "cancel_attempt": "attempt",
    "cancel_item": "item",
    "set_max_parallel": "max_parallel",
}


@given(
    act=st.sampled_from(sorted(_OWN_FIELD)),
    given_fields=st.sets(st.sampled_from(sorted(_FIELDS))),
    notice=st.booleans(),
)
@example(act="cancel_attempt", given_fields={"attempt"}, notice=True)
@example(act="set_max_parallel", given_fields={"max_parallel"}, notice=True)
@example(act="cancel_item", given_fields={"item"}, notice=False)
@example(act="cancel_item", given_fields={"item", "attempt"}, notice=True)
@example(act="cancel_attempt", given_fields=set(), notice=True)
def test_an_action_names_exactly_its_subject_and_leaves_a_notice(
    *, act: str, given_fields: set[str], notice: bool
) -> None:
    fields: dict[str, object] = {}
    for name in given_fields:
        fields.update(_FIELDS[name])
    payload = {"control_id": core.ControlId(root="c"), "action": act, **fields}
    if notice:
        payload["artifact"] = NOTICE
    if given_fields == {_OWN_FIELD[act]} and notice:
        core.ControlInput.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            core.ControlInput.model_validate(payload)


@pytest.mark.parametrize("action", ["pause", "stop", "withdraw"])
def test_only_a_steer_or_an_operator_action_carries_a_notice(action: str) -> None:
    extras: dict[str, dict[str, object]] = {
        "pause": {},
        "stop": {"mode": core.StopMode.DRAIN},
        "withdraw": {"withdraws": core.InputId(root="i")},
    }
    extra = extras[action]
    with pytest.raises(ValidationError, match="artifact"):
        core.ControlInput.model_validate(
            {"control_id": core.ControlId(root="c"), "action": action, "artifact": NOTICE, **extra}
        )


_PHASES = st.lists(st.sampled_from(sorted(core.AttemptPhase)), max_size=6)


def _at_item(phases: list[core.AttemptPhase]) -> tuple[core.CoreState, core.AttemptView]:
    """The running state of ``_state_with_attempt`` plus one attempt per phase at its item."""
    state, attempt = _state_with_attempt()
    [running] = [row for row in state.attempts.attempts if row.attempt_id.root == attempt]
    extra = tuple(
        running.model_copy(
            update={"attempt_id": core.AttemptId(root=f"{attempt}-{index}"), "phase": phase}
        )
        for index, phase in enumerate(phases)
    )
    rows = (*state.attempts.attempts, *extra)
    return state.model_copy(
        update={"attempts": state.attempts.model_copy(update={"attempts": rows})}
    ), running


@given(_PHASES)
@example([core.AttemptPhase.PARKED, core.AttemptPhase.BLOCKED, core.AttemptPhase.CLOSING])
@example([core.AttemptPhase.QUEUED, core.AttemptPhase.TERMINAL])
def test_cancelling_an_item_stops_what_runs_and_lets_parked_and_settling_attempts_be(
    phases: list[core.AttemptPhase],
) -> None:
    """D220: running attempts are withdrawn; parked and settling ones are left as they are.

    ``item_cancellation`` sorts every attempt at the item that has not ended into exactly one
    of the three, and core withdraws exactly the ``stopped`` ones.
    """
    state, running = _at_item(phases)
    item = running.item_id
    rows = {row.attempt_id: row for row in state.attempts.attempts if row.item_id == item}
    split = core.item_cancellation(state, item)
    phase = {name: row.phase for name, row in rows.items()}

    assert set(split.stopped) == {
        name
        for name, value in phase.items()
        if value
        in (core.AttemptPhase.QUEUED, core.AttemptPhase.ACQUIRING, core.AttemptPhase.ACTIVE)
    }
    assert set(split.parked) == {
        name
        for name, value in phase.items()
        if value in (core.AttemptPhase.PARKED, core.AttemptPhase.BLOCKED)
    }
    assert set(split.settling) == {
        name for name, value in phase.items() if value == core.AttemptPhase.CLOSING
    }

    control = _action("op", "cancel_item", item=item.root)
    after = core.step(state, core.RunControlEvent(control=control, now_at=state.run.now_at)).state
    withdrawn = {
        row.decision.target.attempt_id
        for row in after.run.receipts
        if isinstance(row.decision, core.Withdraw)
        and isinstance(row.decision.target, core.AttemptRef)
    }
    assert withdrawn == set(split.stopped)
    left = {row.attempt_id: row for row in after.attempts.attempts}
    for name in (*split.parked, *split.settling):
        assert left[name] == rows[name], name


def test_a_parked_attempt_at_a_cancelled_item_is_not_reopened() -> None:
    """D220: core refuses a search's reopening of a parked attempt at a cancelled item."""

    state, _, reopen = reopen_fixture()
    normalization = reopen.normalized_scope_reopen
    assert normalization is not None
    parked = core.AttemptView(
        attempt_id=normalization.attempt.attempt_id,
        generation=normalization.attempt.generation,
        item_id=core.ItemId(root="I-parked"),
        phase=core.AttemptPhase.PARKED,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
    )
    state = state.model_copy(
        update={"attempts": state.attempts.model_copy(update={"attempts": (parked,)})}
    )

    def reopened(state: core.CoreState) -> tuple[core.StrategyEvent, ...]:
        submitted = core.DecisionSubmitted(
            decision=core.Wait(request=reopen, until=core.Within(limit=100.0)),
            expected_revision=state.revision,
        )
        return core.step(state, submitted).events

    # Uncancelled, the reopen passes validation and reaches the continuation it names, which
    # this state does not hold.
    with pytest.raises(core.ContractError, match="parked continuation"):
        reopened(state)
    control = _action("op", "cancel_item", item=parked.item_id.root)
    state = core.step(state, core.RunControlEvent(control=control, now_at=state.run.now_at)).state
    [rejected] = [e for e in reopened(state) if isinstance(e, core.Rejected)]
    assert rejected.path == ("normalized_scope_reopen", "attempt")
    assert rejected.code is core.RejectionCode.CLOSED_SCOPE


def _queued_reopen() -> tuple[core.CoreState, core.AttemptReopenRequest]:
    """A parked attempt whose reopening the search asked for waits in the admission queue."""
    state, event, _ = queued_reopen_fixture()
    assert event.request.request_id is not None
    queued = core.AttemptReopenRequest(
        decision_id=core.DecisionId(root="reopen"),
        request_id=event.request.request_id,
        attempt=event.normalization.attempt,
    )
    scheduling = state.scheduling.model_copy(update={"slots": (), "queue": (queued,)})
    return state.model_copy(update={"scheduling": scheduling}), queued


def test_cancelling_an_item_retires_a_reopening_already_queued_at_it() -> None:
    """A parked attempt waiting to reopen at a cancelled item never runs again.

    The reopening passed the reopen fence before the cancel, so only retiring it stops it:
    the attempt ends cancelled, the queue no longer holds it, and its admission arriving
    later reopens nothing. The notice names it.
    """
    state, queued = _queued_reopen()
    [parked] = state.attempts.attempts
    assert parked.phase == core.AttemptPhase.PARKED
    split = core.item_cancellation(state, parked.item_id)
    assert (split.unqueued, split.parked) == ((parked.attempt_id,), ())

    control = _action("op", "cancel_item", item=parked.item_id.root)
    cancel = core.RunControlEvent(control=control, now_at=state.run.now_at)
    after = core.step(state, cancel, reducers=QUEUED_REOPEN_REDUCERS).state

    [ended] = after.attempts.attempts
    assert ended.phase == core.AttemptPhase.TERMINAL
    assert after.scheduling.queue == ()
    admitted = core.ScopeReopenAdmitted(
        attempt=queued.attempt, request_id=queued.request_id, admission_id=queued.decision_id
    )
    later = core.step(after, admitted, reducers=QUEUED_REOPEN_REDUCERS)
    assert later.state.attempts == after.attempts
    assert later.requests == ()


def test_cancelling_a_parked_attempt_retires_its_queued_reopening() -> None:
    """D224: a parked attempt waiting to reopen is cancelled as ``cancel_item`` would.

    Core takes the cancel, retires the queued reopening, and the attempt ends cancelled; its
    admission arriving later reopens nothing. A parked attempt with nothing queued is still
    refused (``test_a_cancel_of_a_parked_attempt_is_refused_with_why``).
    """
    state, queued = _queued_reopen()
    [parked] = state.attempts.attempts
    control = _action("op", "cancel_attempt", attempt=parked.attempt_id.root)
    assert core.control_refusal(state, control) is None

    cancel = core.RunControlEvent(control=control, now_at=state.run.now_at)
    after = core.step(state, cancel, reducers=QUEUED_REOPEN_REDUCERS).state

    [ended] = after.attempts.attempts
    assert ended.phase == core.AttemptPhase.TERMINAL
    assert after.scheduling.queue == ()
    admitted = core.ScopeReopenAdmitted(
        attempt=queued.attempt, request_id=queued.request_id, admission_id=queued.decision_id
    )
    later = core.step(after, admitted, reducers=QUEUED_REOPEN_REDUCERS)
    assert later.state.attempts == after.attempts
    assert later.requests == ()


@dataclass
class _CancelWhileWaiting(ToyHost):
    """The operator cancels an attempt while its turn is suspended on its own evaluation.

    It acts at the first poll of the job the suspended turn waits on, which reports running
    for far longer than the run lasts unless core ends it: ``polls_after`` counts the polls
    of that job after the cancel.
    """

    sent: list[core.ControlInput] = field(default_factory=list)
    job: core.ResourceId | None = None
    polls_after: int = 0

    def during_turn(
        self, request: core.Request, state: core.CoreState
    ) -> tuple[core.CoreEvent, ...]:
        events = super().during_turn(request, state)
        if not isinstance(request, core.ObserveOwnedJob):
            return events
        if self.sent:
            self.polls_after += int(request.resource_id == self.job)
            return events
        if not any(
            row.phase == core.ContinuationPhase.WAITING for row in state.evaluation.continuations
        ):
            return events
        owned = next(
            row
            for row in state.evaluation.jobs
            if isinstance(row, core.OwnedJob) and row.resource_id == request.resource_id
        )
        control = _action("op", "cancel_attempt", attempt=owned.scope.owner.root)
        self.sent.append(control)
        self.job = request.resource_id
        return (core.RunControlEvent(control=control, now_at=state.run.now_at), *events)


@pytest.mark.regression
@pytest.mark.xfail(strict=True, reason="S191: a cancel does not end a suspended turn")
@pytest.mark.parametrize("role", ["implementer", "judge"])
def test_a_cancel_ends_a_turn_suspended_on_its_evaluation_and_withdraws_the_evaluation(
    role: str,
) -> None:
    """S191: a cancelled attempt whose turn waited left the search waiting for a resume.

    Core retired the wait silently, so the suspended turn heard nothing until the run's
    deadline and its slot stayed held. The ask must end at once with a typed refusal, the job
    it waited on must be stopped, and the attempt must close cancelled.
    """
    host = _CancelWhileWaiting(waits=(True,), polls=(10_000,), roles=(role,))

    trace = run_toy(host, Config(workstreams=2, in_flight=1))

    assert host.sent, "no turn suspended on its evaluation"
    assert trace.finished, "the run did not end"
    (control,) = host.sent
    name = _cancelled(control)
    (cancelled,) = [item for item in summary(trace).done if item.key == name]
    assert cancelled.failed == (role,)
    assert host.polls_after <= 1, "the evaluation the turn waited on was not withdrawn"
    refused = [
        event
        for event in trace.events
        if isinstance(event, core.ResumeRefused)
        and event.reason == core.ResumeRefusal.ATTEMPT_CLOSED
    ]
    assert len(refused) == 1
    # the slot went to the other workstream, which ran before the run's deadline
    other = next(item for item in summary(trace).done if item.key != name)
    assert other.opened
    assert other.closed
