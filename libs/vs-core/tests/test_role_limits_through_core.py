"""A role governs its session on the real core, and no role can lift core's own bounds.

The toy search runs through the real kernel against scripted executors (``toy_agent_search``).
A role asks for more corrections or more time than the run allows, or for less; the trace shows
how many turns core was asked for, with which limit and model, and the run still ends with every
workspace closed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import sample_retries
from vs_sim.api.testing import examples

from .test_agent_turn_runs import assert_ended_closed
from .toy_agent_search import (
    IMPLEMENTER,
    JUDGE,
    LIMITS,
    ToyHost,
    run_toy,
    summary,
    toy_search,
)

if TYPE_CHECKING:
    from vs_core.api.testing import Trace

_SETTINGS = settings(
    max_examples=examples(pr=3, nightly=12), deadline=None, derandomize=True, database=None
)


def implementer_turns(trace: Trace) -> list[core.DispatchTurn]:
    """The turns core was asked to dispatch for the implementer, in order."""
    return [
        request
        for request in trace.requests
        if isinstance(request, core.DispatchTurn)
        and request.turn.session.role_id.root == "toy-implementer"
    ]


def waits_for(trace: Trace, role: str) -> list[core.Wait]:
    """The waits the search opened to ask ``role``."""
    return [
        decision
        for decision in (getattr(item, "decision", item) for item in trace.decisions)
        if isinstance(decision, core.Wait)
        and isinstance(decision.request, core.RequestTurn)
        and decision.request.turn.session.role_id.root == f"toy-{role}"
    ]


# -- corrections --------------------------------------------------------------------------------------------


@_SETTINGS
@given(corrections=st.integers(0, core.MAX_CORRECTIONS))
def test_an_invalid_reply_is_corrected_as_often_as_the_role_says(corrections: int) -> None:
    implementer = IMPLEMENTER.model_copy(update={"corrections": corrections})
    trace = run_toy(ToyHost(junk=("implementer",)), definition=toy_search(implementer=implementer))

    assert_ended_closed(trace)
    assert len(implementer_turns(trace)) == 1 + corrections
    assert summary(trace).done[0].failed == ("implementer",)


@_SETTINGS
@given(retries=st.integers(0, core.MAX_REQUEST_RETRIES))
def test_the_backends_request_retries_do_not_bound_a_roles_corrections(retries: int) -> None:
    """One field per meaning: corrections are the role's, whatever the backend re-sends."""
    asks = core.MAX_CORRECTIONS
    limits = LIMITS.model_copy(update={"request_retries": sample_retries(retries)})
    implementer = IMPLEMENTER.model_copy(update={"corrections": asks})

    trace = run_toy(
        ToyHost(junk=("implementer",)),
        limits=limits,
        definition=toy_search(implementer=implementer),
    )

    assert_ended_closed(trace, limits)
    assert len(implementer_turns(trace)) == 1 + asks
    assert 1 + asks <= 1 + core.MAX_REASK_CHAIN, "core's hard cap sits above every role's bound"


# -- the time of a turn -------------------------------------------------------------------------------------


@_SETTINGS
@given(
    turn_time=st.floats(min_value=1.0, max_value=core.MAX_TURN_SECONDS, allow_nan=False),
    judge_time=st.floats(min_value=1.0, max_value=core.MAX_TURN_SECONDS, allow_nan=False),
)
def test_every_turn_core_is_asked_for_is_bounded_by_the_time_of_its_role(
    turn_time: float, judge_time: float
) -> None:
    implementer = IMPLEMENTER.model_copy(update={"turn_time": turn_time})
    judge = JUDGE.model_copy(update={"turn_time": judge_time})

    trace = run_toy(ToyHost(), definition=toy_search(implementer=implementer, judge=judge))

    assert_ended_closed(trace)
    (implementing,) = waits_for(trace, "implementer")
    (judging,) = waits_for(trace, "judge")
    assert implementing.until.limit == turn_time
    assert judging.until.limit == judge_time


# -- the operator's model -----------------------------------------------------------------------------------


@_SETTINGS
@given(model=st.sampled_from(["model-a", "model-b"]))
def test_the_model_chosen_for_a_role_reaches_its_turns_and_no_other_role(model: str) -> None:
    definition = toy_search().with_models({"implementer": model})

    trace = run_toy(ToyHost(), definition=definition)

    assert_ended_closed(trace)
    models = {
        request.turn.session.role_id.root: request.turn.session.model
        for request in trace.requests
        if isinstance(request, core.DispatchTurn)
    }
    assert models == {"toy-implementer": model, "toy-judge": None}
