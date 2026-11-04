"""The planner and the workstreams work while the input is measured; nothing compares with it before.

On a shared cluster the input's trusted measurement may wait hours for a slot. The search asks the
planner at once, telling it the measurement is pending, and starts what it schedules: implementers
write code meanwhile, and their candidates are reviewed. A candidate's trusted measurement waits
for the input's (an executor admits jobs in its own order, and a measurement's deadline runs from
its dispatch), and so does what compares a candidate with the input: no candidate is settled or
named the winner before the input's measurement lands. A workstream that ends with a failure is
never compared, so it settles at once and frees its slot. A planning call that ends empty
meanwhile is not a stuck planner: planning is asked again when the measurement lands. An input
that fails its measurement drops a plan made without knowing it that has not started, and the
planner is asked again with the failure in view; a workstream already running goes on, and its
implementer is told the failure. An input with nothing configured to measure it is never shown as
pending. A stop before it lands leaves the input standing. How each measurement outcome ends the
run is unchanged (`test_unmeasurable_input.py` plays it in both orders).
"""

from __future__ import annotations

from tests.vibesys.orchestration.dynamic._host import (
    FACTS,
    PLANNER_LABEL,
    UNMEASURED_TERMS,
    Host,
    begin,
    config,
)
from tests.vibesys.orchestration.dynamic._replies import implement, implemented, plan_reply
from tests.vibesys.orchestration.dynamic.core_policy._contexts import render
from tests.vibesys.search_faults import assert_flow_closed
from tests.vibesys.vibesys_flows import across_restarts, start_flow

from vibesys.orchestration.dynamic.models import ImplementerResult
from vibesys.orchestration.dynamic.search.api import (
    ImplementPrompt,
    PlannerPrompt,
    PromptTemplate,
    dynamic,
)
from vs_core.api import (
    FailureReason,
    Finish,
    Measure,
    RequestTurn,
    Retained,
    RevisionRef,
    StartAttempt,
    Wait,
    Withdraw,
)

MODES = across_restarts
MAX_EMPTY_ASKS = 8
"""More planner turns than one planning call asks before it ends empty (corrections, a fresh ask)."""


def _starts(host: Host) -> int:
    return sum(
        isinstance(item, Wait) and isinstance(item.request, StartAttempt)
        for item in host.run.decisions
    )


def _candidate_measurements(host: Host) -> int:
    return sum(
        isinstance(item, Wait)
        and isinstance(item.request, Measure)
        and item.request.plan.purpose != "baseline"
        for item in host.run.decisions
    )


def _reviews(host: Host) -> int:
    return sum(
        isinstance(item, Wait)
        and isinstance(item.request, RequestTurn)
        and "/judge." in item.request.decision_id.root
        for item in host.run.decisions
    )


@MODES
def test_the_planner_is_asked_before_a_slow_input_measurement_lands(*, restarting: bool) -> None:
    # Two evaluation slots: one is free beside the input's, yet no candidate takes it.
    cfg = config(max_in_flight=2, max_rounds=2, evaluation_slots=2)
    host = begin(cfg, restarting=restarting, input_first=False)

    # The first planner turn is open while the input's measurement is.
    first = host.run.expect_turn(role=PLANNER_LABEL).context
    (measuring,) = host.input_waits()
    assert isinstance(measuring.request, Measure)
    assert measuring.request.plan.purpose == "baseline"
    assert isinstance(first, PlannerPrompt)
    assert first.baseline_pending
    assert first.baseline == ()
    assert first.input_failure is None
    assert "still being measured" in render(PromptTemplate.PORTFOLIO, first)

    # What the planner schedules starts at once, built on the input, and the implementers work.
    host.run.reply_raw(
        host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("h1"), implement("h2"))
    )
    starts = host.open(StartAttempt)
    assert len(starts) == 2
    for item in starts:
        assert isinstance(item.request, StartAttempt)
        assert item.request.workspace.base == FACTS.baseline
        host.answer(item)
    for commit in ("c1", "c2"):
        turn = host.run.expect_turn(role="implementer")
        assert isinstance(turn.context, ImplementPrompt)
        assert turn.context.baseline_pending  # told that its candidate's evaluation waits
        assert "still queued or running" in render(PromptTemplate.IMPLEMENT, turn.context)
        host.run.reply(
            turn,
            ImplementerResult.model_validate_json(implemented()),
            checkpoint=Retained(revision=RevisionRef.of_git_commit(commit)),
        )

    # The candidates are reviewed before the input lands, but not measured: an executor admits
    # jobs in its own order, so a candidate dispatched while the input's job waits would spend
    # its deadline in that queue. Nothing settles and no candidate is named the winner.
    host.drain_beside_input()
    assert host.input_waits()
    assert _reviews(host) == 2
    assert _candidate_measurements(host) == 0
    record = host.record
    assert record is not None
    assert not record.finished
    assert host.run.finished is None

    # When the measurement lands, both candidates are measured and settled against it.
    host.measure_input()
    host.drain()
    measured = {
        item.request.plan.candidate
        for item in host.run.decisions
        if isinstance(item, Wait) and isinstance(item.request, Measure)
    }
    assert {RevisionRef.of_git_commit("c1"), RevisionRef.of_git_commit("c2")} <= measured
    assert isinstance(host.run.finished, Finish)
    record = host.record
    assert record is not None
    assert {"h1", "h2"} <= {item.plan.work_id for item in record.finished}
    assert_flow_closed(host)


@MODES
def test_a_planning_call_that_ends_empty_before_the_input_lands_is_asked_again_after_it(
    *, restarting: bool
) -> None:
    host = begin(config(), restarting=restarting, input_first=False)
    for _ in range(MAX_EMPTY_ASKS):
        turns = host.open(RequestTurn, label=PLANNER_LABEL)
        if not turns:
            break
        host.run.fail(turns[0], FailureReason.TIMED_OUT)
    else:
        message = f"the planning call never ended: {host.run.timeline()}"
        raise AssertionError(message)

    # Nothing in flight but the input's measurement: the planner is not given up on.
    assert host.input_waits()
    assert host.run.finished is None
    host.measure_input()
    later = host.run.expect_turn(role=PLANNER_LABEL).context
    assert isinstance(later, PlannerPrompt)
    assert not later.baseline_pending
    assert later.baseline != ()


@MODES
def test_a_stop_before_the_input_lands_starts_nothing_and_leaves_the_input_standing(
    *, restarting: bool
) -> None:
    host = begin(config(max_in_flight=2), restarting=restarting, input_first=False)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("h1")))
    host.run.stop()
    host.drain()

    assert _starts(host) == _candidate_measurements(host) == 0
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.selection is not None
    assert finish.result.selection.kind == "trusted_baseline"
    assert_flow_closed(host)


@MODES
def test_a_workstream_started_before_the_input_fails_its_measurement_goes_on(
    *, restarting: bool
) -> None:
    host = begin(
        config(max_rounds=2), restarting=restarting, input_first=False, baseline="failed-workload"
    )
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("h1")))
    (start,) = host.open(StartAttempt)

    host.measure_input()

    # The work already paid for goes on, and its implementer is told why the input failed.
    assert host.open(StartAttempt) == [start]
    host.answer(start)
    told = host.run.expect_turn(role="implementer").context
    assert isinstance(told, ImplementPrompt)
    assert not told.baseline_pending
    assert told.input_failure is not None
    text = render(PromptTemplate.IMPLEMENT, told)
    assert "trusted measurement failed: the input's own benchmark failed" in text
    assert "still queued or running" not in text
    # The next planner turn is told too.
    host.drain_until_turn(PLANNER_LABEL)
    assert _candidate_measurements(host) == 1
    again = host.run.expect_turn(role=PLANNER_LABEL).context
    assert isinstance(again, PlannerPrompt)
    assert not again.baseline_pending
    assert again.input_failure is not None
    host.drain()
    assert host.run.finished is not None
    assert_flow_closed(host)


@MODES
def test_a_planning_call_in_flight_when_the_input_fails_is_planned_again(
    *, restarting: bool
) -> None:
    host = begin(config(), restarting=restarting, input_first=False, baseline="failed-workload")
    blind = host.run.expect_turn(role=PLANNER_LABEL)

    host.measure_input()
    host.run.reply_raw(blind, plan_reply(implement("h1")))

    assert not host.open(StartAttempt)
    again = host.run.expect_turn(role=PLANNER_LABEL).context
    assert isinstance(again, PlannerPrompt)
    assert again.input_failure is not None


@MODES
def test_an_input_with_nothing_to_measure_is_never_shown_as_pending(*, restarting: bool) -> None:
    cfg = config()
    facts = FACTS.model_copy(update={"task": UNMEASURED_TERMS})
    host = Host(start_flow(dynamic, cfg, facts=facts, restarting=restarting), cfg)

    assert not host.input_waits()
    first = host.run.expect_turn(role=PLANNER_LABEL).context
    assert isinstance(first, PlannerPrompt)
    assert not first.baseline_pending
    assert "still being measured" not in render(PromptTemplate.PORTFOLIO, first)


@MODES
def test_a_workstream_that_fails_before_the_input_lands_settles_at_once_and_frees_its_slot(
    *, restarting: bool
) -> None:
    host = begin(config(max_rounds=2), restarting=restarting, input_first=False)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("h1")))
    (start,) = host.open(StartAttempt)
    host.answer(start)

    # The implementer makes a candidate it says failed, then its session fails: the workstream
    # ends with a failure while carrying a candidate, which makes it ineligible however it would
    # measure.
    host.run.reply(
        host.run.expect_turn(role="implementer"),
        ImplementerResult.model_validate_json(implemented("implementation_failed")),
        checkpoint=Retained(revision=RevisionRef.of_git_commit("c1")),
    )
    host.run.fail(host.run.expect_turn(role="implementer"), FailureReason.OUT_OF_BUDGET)

    # It settles now, while the input is still being measured, and frees its slot.
    assert host.input_waits()
    (settle,) = host.open(Withdraw)
    host.answer(settle)
    record = host.record
    assert record is not None
    assert not record.running
    assert [done.failure is not None for done in record.finished] == [True]
    assert _candidate_measurements(host) == 0
    assert not host.open(RequestTurn, label="judge")
    host.measure_input()
    host.drain()
    assert isinstance(host.run.finished, Finish)
    assert_flow_closed(host)
