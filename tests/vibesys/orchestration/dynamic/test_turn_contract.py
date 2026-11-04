"""What dynamic asks of core and answers back, in ways the flow stories do not already pin.

Core owns the rules of a turn (how often a reply is corrected and in which session, what an
unrecorded wait is, how an adoption is retried or given up). These tests pin the dynamic half:
that every turn it asks carries a template of its own role and a typed context, and that a reply
waiting for something nothing recorded ends the turn and not the run.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import (
    PLANNER_LABEL,
    Host,
    begin,
    config,
)
from tests.vibesys.orchestration.dynamic._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.search_faults import assert_flow_closed
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.dynamic.models import ImplementerResult, ReviewResult
from vibesys.orchestration.dynamic.search.api import (
    IMPLEMENTER,
    JUDGE,
    ROLES,
    ImplementPrompt,
    PlannerCorrection,
    PlannerPrompt,
    ProfilePrompt,
    ReviewPrompt,
)
from vs_core.api import (
    Finish,
    RequestTurn,
    Retained,
    RevisionRef,
    StartAttempt,
    Wait,
)
from vs_core.api.search import ReplyCorrection, WaitUnrecorded
from vs_sim.api.testing import examples

MODES = across_restarts
CANDIDATE = RevisionRef.of_git_commit("c1")
NOT_JSON = "not json"


def to_the_turn_of(host: Host, role: str) -> None:
    """Plan one hypothesis, open its workspace and (for the judge) let the implementer finish."""
    host.drain_until_turn(PLANNER_LABEL)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("h1")))
    host.answer(host.run.expect(StartAttempt, label="start"))
    if role == "judge":
        result = ImplementerResult.model_validate_json(implemented())
        host.run.reply(
            host.run.expect_turn(role="implementer"),
            result,
            checkpoint=Retained(revision=CANDIDATE),
        )


# -- a reply that waits for something nothing recorded ----------------------------------------------


def wait_on(kind: str) -> str:
    return json.dumps({"kind": kind, "handles": ["eval_1"]})


@MODES
@pytest.mark.parametrize("role", ["implementer", "judge"])
@pytest.mark.parametrize("kind", ["waiting_for_profiler", "waiting_for_evaluation", "unknown_wait"])
def test_a_wait_the_run_never_recorded_ends_its_turn_whichever_wait_it_names(
    *, restarting: bool, role: str, kind: str
) -> None:
    """Regression: a `waiting_for_profiler` reply, valid under the implementer schema, crashed the run.

    A wait nothing recorded (these turns never submitted an evaluation) or a kind the schema
    rejects is told to the agent once, as the exchange of its kind; the agent may then answer
    properly and the run ends with everything closed.
    """
    host = begin(config(), restarting=restarting)
    to_the_turn_of(host, role)
    asked = host.run.expect_turn(role=role)
    host.run.reply_raw(asked, wait_on(kind))

    follow = host.run.expect_turn(role=role)
    unrecorded = kind == "waiting_for_evaluation"
    exchanges = IMPLEMENTER.exchanges if role == "implementer" else JUDGE.exchanges
    assert follow.template == (exchanges.wait_unrecorded if unrecorded else exchanges.correction)
    assert isinstance(follow.context, WaitUnrecorded if unrecorded else ReplyCorrection)
    assert isinstance(follow.request, RequestTurn)
    assert follow.request.turn.charge_class == "correction"
    assert follow.request.turn.session == asked.request.turn.session  # ty: ignore[unresolved-attribute]

    if role == "implementer":
        result = ImplementerResult.model_validate_json(implemented())
        host.run.reply(follow, result, checkpoint=Retained(revision=CANDIDATE))
        host.run.reply(
            host.run.expect_turn(role="judge"), ReviewResult.model_validate_json(reviewed())
        )
    else:
        host.run.reply(follow, ReviewResult.model_validate_json(reviewed()))
    host.drain()

    assert isinstance(host.run.finished, Finish)
    assert_flow_closed(host)


# -- every turn the search asks ----------------------------------------------------------------------

PROMPTS = {role.name: role.prompt for role in ROLES}
EXCHANGE_TEMPLATES = {
    name
    for role in ROLES
    for name in (role.exchanges.correction, role.exchanges.resume, role.exchanges.wait_unrecorded)
    if name is not None
}
TURN_CONTEXTS = (
    PlannerPrompt,
    PlannerCorrection,
    ImplementPrompt,
    ReviewPrompt,
    ProfilePrompt,
    ReplyCorrection,
    WaitUnrecorded,
)
STEPS = st.lists(st.tuples(st.integers(0, 7), st.integers(0, 11)), max_size=40)


def check_asked_turns(host: Host) -> None:
    """Every open turn carries a template of the search's roles and a typed context."""
    for asked in host.run.open:
        if isinstance(asked.request, RequestTurn):
            assert asked.template in {*PROMPTS.values(), *EXCHANGE_TEMPLATES}, asked
            assert isinstance(asked.context, TURN_CONTEXTS)


@MODES
@settings(max_examples=examples(pr=10, nightly=40), deadline=None, derandomize=True, database=None)
@given(steps=STEPS, in_flight=st.integers(1, 2))
def test_every_turn_is_asked_with_its_roles_template_or_an_exchange_and_a_session_that_never_changes(
    *, restarting: bool, steps: list[tuple[int, int]], in_flight: int
) -> None:
    host = begin(config(max_rounds=2, max_in_flight=in_flight), restarting=restarting)
    for pick, variant in steps:
        check_asked_turns(host)
        if not host.step(pick, variant):
            break
    while True:
        check_asked_turns(host)
        if not host.step(0):
            break

    seen: dict[str, object] = {}
    for item in host.run.decisions:
        if isinstance(item, Wait) and isinstance(item.request, RequestTurn):
            turn = item.request.turn
            assert seen.setdefault(turn.session.session_id.root, turn.session) == turn.session
            if turn.charge_class == "correction":
                assert turn.predecessor is not None
                assert turn.predecessor.session_id == turn.session.session_id
