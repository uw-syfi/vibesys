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
- core refuses an action exactly when ``action_refusal`` says it does, and a replay of an
  action changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

import vs_core.api as core
from vs_core.testing.drive import Trace

from .toy_agent_search import LIMITS, Config, ToyHost, run_toy

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
    attempt ids). It is sent only if core would take it then (``action_refusal``), so ``sent``
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
        if core.action_refusal(state, control) is not None:
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


def test_a_raised_parallel_limit_admits_what_now_fits() -> None:
    narrow = LIMITS.model_copy(update={"max_parallel": 1})
    held = OperatorHost(at=99)
    run_toy(held, Config(workstreams=3, in_flight=3), limits=narrow)
    raised = OperatorHost(at=0, act="set_max_parallel", limit=3)
    trace = run_toy(raised, Config(workstreams=3, in_flight=3), limits=narrow)

    assert trace.finished
    assert max(held.slots) == 1
    assert max(raised.slots) > 1


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
def test_core_refuses_an_action_exactly_when_action_refusal_does(
    act: _Act, known: bool, twice: bool
) -> None:
    state, attempt = _state_with_attempt()
    subject = attempt if known else "nothing"
    control = {
        "cancel_attempt": _action("a", "cancel_attempt", attempt=subject),
        "cancel_item": _action("a", "cancel_item", item=subject),
        "set_max_parallel": _action("a", "set_max_parallel", max_parallel=2),
    }[act]
    refusal = core.action_refusal(state, control)
    assert (refusal is None) == (known or act == "set_max_parallel")
    event = core.RunControlEvent(control=control, now_at=state.run.now_at)
    if refusal is not None:
        with pytest.raises(core.ContractError) as raised:
            core.step(state, event)
        assert raised.value.path == refusal.path
        return
    after = core.step(state, event).state
    if twice:
        # The same control again is a replay: it changes nothing.
        assert core.step(after, event).state == after
        return
    # A second, different cancel of what is already cancelled is refused by the same rule.
    again = control.model_copy(update={"control_id": core.ControlId(root="b")})
    if act == "set_max_parallel":
        assert core.action_refusal(after, again) is None
    else:
        assert core.action_refusal(after, again) is not None


_FIELDS: dict[str, dict[str, object]] = {
    "attempt": {"attempt": core.AttemptId(root="a")},
    "item": {"item": core.ItemId(root="i")},
    "max_parallel": {"max_parallel": 2},
}
_OWN_FIELD = {"cancel_attempt": "attempt", "cancel_item": "item", "set_max_parallel": "max_parallel"}


@given(
    act=st.sampled_from(sorted(_OWN_FIELD)),
    given_fields=st.sets(st.sampled_from(sorted(_FIELDS))),
    notice=st.booleans(),
)
def test_an_action_names_exactly_its_subject_and_leaves_a_notice(
    act: str, given_fields: set[str], notice: bool
) -> None:
    fields: dict[str, object] = {}
    for name in given_fields:
        fields.update(_FIELDS[name])
    payload = {"control_id": "c", "action": act, **fields}
    if notice:
        payload["artifact"] = NOTICE
    if given_fields == {_OWN_FIELD[act]} and notice:
        core.ControlInput.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            core.ControlInput.model_validate(payload)


@pytest.mark.parametrize("action", ["pause", "stop", "withdraw"])
def test_only_a_steer_or_an_operator_action_carries_a_notice(action: str) -> None:
    extra: dict[str, object] = {
        "pause": {"owner": core.PauseOwner.OPERATOR},
        "stop": {"mode": core.StopMode.DRAIN},
        "withdraw": {"withdraws": core.InputId(root="i")},
    }[action]
    with pytest.raises(ValidationError, match="artifact"):
        core.ControlInput.model_validate(
            {"control_id": "c", "action": action, "artifact": NOTICE, **extra}
        )
