"""Each planning call that schedules a plan leaves one handoff file, which later planners read.

A planning call starts a new planner session, so what a planner meant is lost unless it is written
down. The search asks the handoff owner to write one file per scheduled planning call, from the
planner's reply; every later planning prompt lists the files' paths, newest first, and never their
contents. These tests play the search under a fake host whose owner keeps the files, live and with
restarts, and check the prompts the planner is asked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given, note, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import (
    PLANNER_LABEL,
    Host,
    begin,
    config,
    handoff_path,
)
from tests.vibesys.orchestration.dynamic._replies import expectation, implement, plan_reply
from tests.vibesys.orchestration.dynamic._slots import (
    implement_and_judge,
    open_next_slot,
    planner_context,
)
from tests.vibesys.orchestration.dynamic.core_policy._contexts import render
from tests.vibesys.search_faults import Step, play, steps
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.dynamic_handoff import render_handoff
from vibesys.orchestration.dynamic.search.api import (
    PlannerCorrection,
    PlannerPrompt,
    PromptTemplate,
    ViolationCode,
    WriteHandoff,
)
from vs_core.api import FailureReason, Measure, Operation, RequestTurn, Wait
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from vs_core.api.testing import Asked

MODES = across_restarts


def _written_newest_first(host: Host) -> tuple[str, ...]:
    """The paths of the files the owner holds, newest planning call first."""
    calls = sorted((request.call, path) for path, request in host.handoffs.items())
    return tuple(path for _, path in reversed(calls))


def _check_prompt(host: Host) -> None:
    """An open planner turn lists exactly the files written so far, newest first."""
    for asked in host.open(RequestTurn, label=PLANNER_LABEL):
        context = asked.context
        assert isinstance(context, PlannerPrompt | PlannerCorrection)
        assert context.handoff_paths == _written_newest_first(host), host.run.timeline()


def _scheduled_calls(host: Host) -> set[int]:
    """The planning calls whose plan the search scheduled: one handoff write was asked of each."""
    return {
        item.request.request.call
        for item in host.run.decisions
        if isinstance(item, Wait)
        and isinstance(item.request, Operation)
        and isinstance(item.request.request, WriteHandoff)
    }


@MODES
@settings(max_examples=examples(pr=10, nightly=60), deadline=None, derandomize=True, database=None)
@given(story=steps(), rounds=st.integers(1, 3), in_flight=st.integers(1, 2))
def test_every_planner_prompt_lists_every_earlier_handoff_file_newest_first(
    *, restarting: bool, story: list[Step], rounds: int, in_flight: int
) -> None:
    host = begin(config(max_rounds=rounds, max_in_flight=in_flight), restarting=restarting)
    try:
        play(host, story, check=lambda: _check_prompt(host))
    finally:
        note(host.run.timeline())
    # One file per scheduled call, named by the call; a restart never writes another one.
    assert set(host.handoffs) == {handoff_path(call) for call in _scheduled_calls(host)}
    assert host.handoff_writes == len(host.handoffs)


@MODES
@settings(max_examples=examples(pr=5, nightly=30), deadline=None)
@given(rounds=st.integers(2, 4), marker=st.from_regex(r"MARK[a-z0-9]{6}", fullmatch=True))
def test_each_planning_call_adds_one_file_and_the_next_prompt_names_it_but_never_its_contents(
    *, restarting: bool, rounds: int, marker: str
) -> None:
    host = begin(config(max_rounds=rounds, max_in_flight=1), restarting=restarting)
    for call in range(1, rounds + 1):
        host.drain_until_turn(PLANNER_LABEL)
        context = planner_context(host)
        assert isinstance(context, PlannerPrompt)
        listed = tuple(handoff_path(earlier) for earlier in range(call - 1, 0, -1))
        assert context.handoff_paths == listed
        prompt = render(PromptTemplate.PORTFOLIO, context)
        assert all(f"`{path}`" in prompt for path in listed)
        assert marker not in prompt  # what the files say stays in the files
        identifier = f"h{call}"
        note_of: dict[str, object] = {
            "expectations": [expectation(identifier, expected=f"{marker} {call}")],
            "doubts": [f"{marker} doubt {call}"],
        }
        reply = plan_reply(implement(identifier), reasoning=f"{marker} why", handoff=note_of)
        host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), reply)
        assert set(host.handoffs) == {handoff_path(n) for n in range(1, call + 1)}
        written = host.handoffs[handoff_path(call)]
        assert written.reasoning == f"{marker} why"
        assert [item.work_id for item in written.scheduled] == [identifier]
        assert written.handoff.doubts == (f"{marker} doubt {call}",)
        open_next_slot(host)
        implement_and_judge(host, identifier, f"c{call}")
        for asked in host.open(Measure):
            host.answer(asked)


def _writes(host: Host) -> list[Asked]:
    """The handoff writes open now."""
    return [
        asked
        for asked in host.open(Operation)
        if isinstance(asked.request, Operation) and isinstance(asked.request.request, WriteHandoff)
    ]


def _write_request(asked: Asked) -> WriteHandoff:
    assert isinstance(asked.request, Operation)
    request = asked.request.request
    assert isinstance(request, WriteHandoff)
    return request


def _played(host: Host, identifier: str, candidate: str) -> None:
    """Play the scheduled workstream through, up to the next planner turn."""
    open_next_slot(host)
    implement_and_judge(host, identifier, candidate)
    for asked in host.open(Measure):
        host.answer(asked)
    host.drain_until_turn(PLANNER_LABEL)


_UNKNOWN = (FailureReason.TIMED_OUT, FailureReason.LOST)
"""Write failures after which the bytes may have landed: the search sends the write again."""


@MODES
@settings(deadline=None)
@given(
    reason=st.sampled_from((*_UNKNOWN, FailureReason.REFUSED)),
    landed=st.booleans(),
)
def test_a_write_that_fails_for_good_keeps_the_plan_and_a_later_write_lists_what_landed(
    *, restarting: bool, reason: FailureReason, landed: bool
) -> None:
    host = begin(config(max_rounds=3), restarting=restarting, handoff_writes_open=True)
    host.drain_until_turn(PLANNER_LABEL)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("h1")))
    tries = 0
    while writes := _writes(host):
        (write,) = writes
        if landed:
            host.write_handoff(_write_request(write))  # the bytes land, the answer does not
        host.run.fail(write, reason)
        tries += 1
    assert tries == (2 if reason in _UNKNOWN else 1)  # an unknown outcome is sent once more
    _played(host, "h1", "c1")
    context = planner_context(host)
    assert isinstance(context, PlannerPrompt)
    assert context.handoff_paths == ()  # the plan stands; no write reported a listing yet
    assert [row.hypothesis_id for row in context.history if hasattr(row, "hypothesis_id")] == ["h1"]
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("h2")))
    (write,) = _writes(host)
    host.run.finish_operation(write, host.write_handoff(_write_request(write)))
    _played(host, "h2", "c2")
    context = planner_context(host)
    assert isinstance(context, PlannerPrompt)
    # The owner lists every file it holds: the first call's too, once its bytes landed.
    landed_files = (handoff_path(1),) if landed else ()
    assert context.handoff_paths == (handoff_path(2), *landed_files)


@MODES
@settings(deadline=None)
@given(reason=st.sampled_from(_UNKNOWN))
def test_a_write_whose_outcome_was_unknown_is_sent_again_and_listed(
    *, restarting: bool, reason: FailureReason
) -> None:
    host = begin(config(max_rounds=2), restarting=restarting, handoff_writes_open=True)
    host.drain_until_turn(PLANNER_LABEL)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("h1")))
    (write,) = _writes(host)
    host.write_handoff(_write_request(write))
    host.run.fail(write, reason)
    (again,) = _writes(host)
    assert _write_request(again) == _write_request(write)  # the same keyed write, same bytes
    host.run.finish_operation(again, host.write_handoff(_write_request(again)))
    _played(host, "h1", "c1")
    context = planner_context(host)
    assert isinstance(context, PlannerPrompt)
    assert context.handoff_paths == (handoff_path(1),)


@MODES
@settings(max_examples=examples(pr=5, nightly=30), deadline=None)
@given(rejected=st.integers(0, 2))
def test_the_file_states_expectations_only_of_the_workstreams_that_were_scheduled(
    *, restarting: bool, rejected: int
) -> None:
    """A reply of three workstreams with one rejected schedules two: the file says nothing of
    what to do after the rejected one, and names it as dropped."""
    host = begin(config(max_rounds=1, max_in_flight=3), restarting=restarting)
    host.drain_until_turn(PLANNER_LABEL)
    ids = ["h0", "h1", "h2"]
    entries = [
        implement(identifier, continue_hypothesis=True)  # unknown: cannot be continued
        if position == rejected
        else implement(identifier)
        for position, identifier in enumerate(ids)
    ]
    reply = plan_reply(*entries)
    for _ in range(2):  # the reply, then the same reply as its correction
        if not host.open(RequestTurn, label=PLANNER_LABEL):
            break
        host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), reply)
    (written,) = host.handoffs.values()
    scheduled = [item.work_id for item in written.scheduled]
    assert scheduled == [item for position, item in enumerate(ids) if position != rejected]
    assert [item.work_id for item in written.handoff.expectations] == scheduled
    assert written.handoff.dropped == (ids[rejected],)
    text = render_handoff(written)
    assert f"build on {ids[rejected]}" not in text
    assert f"drop {ids[rejected]}" not in text
    assert all(f"build on {item}" in text for item in scheduled)


@MODES
@settings(deadline=None)
@given(stranger=st.from_regex(r"x[0-9]{1,3}", fullmatch=True))
def test_an_expectation_of_no_workstream_of_the_reply_is_corrected_and_left_out(
    *, restarting: bool, stranger: str
) -> None:
    host = begin(config(max_rounds=1), restarting=restarting)
    host.drain_until_turn(PLANNER_LABEL)
    note: dict[str, object] = {
        "expectations": [expectation("h1"), expectation(stranger)],
        "doubts": [],
    }
    reply = plan_reply(implement("h1"), handoff=note)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), reply)
    correction = planner_context(host)
    assert isinstance(correction, PlannerCorrection)
    assert correction.problem is not None
    assert [item.code for item in correction.problem.violations] == [
        ViolationCode.EXPECTATION_UNKNOWN
    ]
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), reply)
    (written,) = host.handoffs.values()
    assert [item.work_id for item in written.handoff.expectations] == ["h1"]
    assert written.handoff.dropped == (stranger,)
