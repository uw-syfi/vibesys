"""Agents keep working while candidates wait for a trusted evaluation, bound only when a slot frees.

A workstream whose candidate is reviewed submits it to the run's evaluation queue and parks: its
agent slot goes to new work at once, whether or not an evaluation slot is free. That is the
default; a run launched with no backlog (``max_backlog=0``) keeps each agent slot until its
workstream ends. Which candidate an evaluation slot measures is decided only when the slot frees: the
newest waiting one. A parked hypothesis is still in flight, so it is not continued until its
result is in. A stop keeps the reviewed code of what it kept from its measurement. A workstream
that waits longer than the run allows for the input's measurement is not left hanging silently:
the orchestrator is told on its next turn. A plan step accepted on its own checks never waits for
the input at all.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import (
    PLANNER_LABEL,
    Host,
    begin,
    config,
    plan_config,
)
from tests.vibesys.orchestration.dynamic._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
    step,
)
from tests.vibesys.orchestration.dynamic.core_policy._contexts import render
from tests.vibesys.search_faults import assert_flow_closed
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.dynamic.models import ImplementerResult, ReviewResult
from vibesys.orchestration.dynamic.search.api import (
    DynamicConfig,
    PlannerCorrection,
    PlannerPrompt,
    PromptTemplate,
    RunSnapshot,
)
from vs_core.api import (
    Finish,
    Measure,
    RequestTurn,
    Retained,
    RevisionRef,
    Settle,
    StartAttempt,
    Wait,
    Withdraw,
)
from vs_sim.api.testing import examples

MODES = across_restarts
QUEUED = {"max_in_flight": 1, "evaluation_slots": 1, "max_backlog": 3}
"""One agent slot and one evaluation slot; up to three workstreams parked on their measurement."""


def _line_of(decision: str) -> str:
    """The workstream a decision belongs to: ``000002.h2/measure.1`` is of ``000002.h2``."""
    return decision.split("/", 1)[0]


def _issued[R](host: Host, kind: type[R]) -> list[R]:
    """Every request of ``kind`` the run issued, in order."""
    return [
        item.request
        for item in host.run.decisions
        if isinstance(item, Wait) and isinstance(item.request, kind)
    ]


def _measured(host: Host) -> list[str]:
    """The workstreams whose candidate the run measured, in the order it asked."""
    return [
        _line_of(request.decision_id.root)
        for request in _issued(host, Measure)
        if request.plan.purpose != "baseline"
    ]


def _measuring(host: Host) -> list[str]:
    """The workstreams whose candidate is being measured now."""
    return [
        _line_of(item.decision.root)
        for item in host.open(Measure)
        if isinstance(item.request, Measure) and item.request.plan.purpose != "baseline"
    ]


def _answer_but_measurements(host: Host) -> None:
    """Answer every open wait but agent turns and trusted measurements (workspaces, checks)."""
    while waits := [
        item for item in host.run.open if not isinstance(item.request, RequestTurn | Measure)
    ]:
        host.answer(waits[0])


def _work(host: Host, line: str, commit: str) -> None:
    """Implement the open turn of workstream ``line`` as ``commit`` and pass the judge's review."""
    _answer_but_measurements(host)
    implementer = next(
        item
        for item in host.open(RequestTurn, label="implementer")
        if _line_of(item.decision.root) == line
    )
    host.run.reply(
        implementer,
        ImplementerResult.model_validate_json(implemented()),
        checkpoint=Retained(revision=RevisionRef.of_git_commit(commit)),
    )
    _answer_but_measurements(host)
    judge = next(
        item
        for item in host.open(RequestTurn, label="judge")
        if _line_of(item.decision.root) == line
    )
    host.run.reply(judge, ReviewResult.model_validate_json(reviewed()))
    _answer_but_measurements(host)


def _end_measurement(host: Host, line: str) -> None:
    """Answer the trusted measurement of ``line``, and what decoding its evidence asks."""
    (measuring,) = [item for item in host.open(Measure) if _line_of(item.decision.root) == line]
    host.answer(measuring)
    _answer_but_measurements(host)


def _plan(host: Host, *entries: dict[str, object]) -> PlannerPrompt:
    """Answer the open planner turn with ``entries``; the prompt it was asked with."""
    turn = host.run.expect_turn(role=PLANNER_LABEL)
    assert isinstance(turn.context, PlannerPrompt)
    host.run.reply_raw(turn, plan_reply(*entries))
    return turn.context


def _record(host: Host) -> RunSnapshot:
    record = host.record
    assert record is not None
    return record


@MODES
@settings(max_examples=examples(pr=4, nightly=20), deadline=None, derandomize=True, database=None)
@given(waiting=st.integers(2, 5))
def test_by_default_the_planner_fills_agent_slots_while_candidates_wait_for_a_measurement(
    *, restarting: bool, waiting: int
) -> None:
    """Regression: the default backlog was 0, so two queued candidates held both agent slots and
    the run sat idle behind a busy evaluation queue instead of planning and implementing."""
    host = begin(DynamicConfig(max_rounds=waiting), restarting=restarting)
    _plan(host, implement("h1"), implement("h2"))
    _work(host, "000001.h1", "c1")
    _work(host, "000002.h2", "c2")
    for n in range(3, waiting + 1):
        _plan(host, implement(f"h{n}"))
        _work(host, f"{n:06d}.h{n}", f"c{n}")

    # Every candidate waits for, or holds, the one evaluation slot; none holds an agent slot.
    assert len(_measuring(host)) == 1
    assert len(_record(host).running) == waiting
    _plan(host, implement("next"))
    _answer_but_measurements(host)
    assert [
        item
        for item in host.open(RequestTurn, label="implementer")
        if _line_of(item.decision.root).endswith(".next")
    ]


@MODES
def test_a_run_launched_without_a_backlog_keeps_each_agent_slot_until_its_workstream_ends(
    *, restarting: bool
) -> None:
    """With ``max_backlog=0`` a run serializes on its measurements, as runs did before parking."""
    host = begin(DynamicConfig(max_rounds=2, max_backlog=0), restarting=restarting)
    _plan(host, implement("h1"), implement("h2"))
    _work(host, "000001.h1", "c1")
    _work(host, "000002.h2", "c2")

    # Both candidates are reviewed and wait for, or hold, their measurement: neither workstream
    # gives up its agent slot, so the planner is not asked for more work meanwhile.
    assert _measuring(host)
    assert not host.open(RequestTurn, label=PLANNER_LABEL)
    assert len(_record(host).running) == 2

    host.drain()
    assert {"000001.h1", "000002.h2"} <= set(_measured(host))
    assert all(item.failure is None for item in _record(host).finished[:2])
    assert isinstance(host.run.finished, Finish)
    assert_flow_closed(host)


@MODES
def test_agents_keep_working_and_a_freed_slot_measures_the_newest_candidate(
    *, restarting: bool
) -> None:
    host = begin(config(max_rounds=3, **QUEUED), restarting=restarting)
    _plan(host, implement("h1"))
    _work(host, "000001.h1", "c1")
    assert _measuring(host) == ["000001.h1"]

    # h1 holds the only evaluation slot, yet its agent slot is free: h2 and h3 are implemented.
    _plan(host, implement("h2"))
    _work(host, "000002.h2", "c2")
    told = _plan(host, implement("h3"))
    _work(host, "000003.h3", "c3")
    assert _measuring(host) == ["000001.h1"]
    assert not _record(host).finished

    # The orchestrator saw the queue: h1 measured, h2 waiting.
    assert told.evaluations is not None
    assert [item.work_id for item in told.evaluations.measuring] == ["h1"]
    assert [item.work_id for item in told.evaluations.waiting] == ["h2"]
    assert "waiting: `h2`" in render(PromptTemplate.PORTFOLIO, told)

    # When h1's measurement ends, its slot is bound to the newest candidate waiting, h3's.
    _end_measurement(host, "000001.h1")
    assert _measuring(host) == ["000003.h3"]
    host.drain()
    assert _measured(host) == ["000001.h1", "000003.h3", "000002.h2"]
    assert isinstance(host.run.finished, Finish)
    assert_flow_closed(host)


@MODES
def test_a_hypothesis_whose_candidate_waits_is_continued_only_once_its_result_is_in(
    *, restarting: bool
) -> None:
    """A continuation records a new round of its hypothesis and builds on the last one, so it waits
    for the round in flight: the planner sees a parked hypothesis as busy."""
    host = begin(config(max_rounds=4, **QUEUED), restarting=restarting)
    _plan(host, implement("h1"))
    _work(host, "000001.h1", "c1")
    _plan(host, implement("h2"))
    _work(host, "000002.h2", "c2")

    # h2 waits for the slot h1 holds; the orchestrator tries to continue it.
    told = _plan(host, implement("h2", continue_hypothesis=True, task="next step"))
    (row,) = [item for item in told.history if getattr(item, "hypothesis_id", "") == "h2"]
    assert row.status == "submitted"
    correction = host.run.expect_turn(role=PLANNER_LABEL).context
    assert isinstance(correction, PlannerCorrection)
    assert correction.problem is not None
    assert [(item.code, item.subject) for item in correction.problem.violations] == [
        ("hypothesis-in-flight", "h2")
    ]
    assert "hypothesis 'h2' is still in flight" in render(
        PromptTemplate.PORTFOLIO_CORRECTION, correction
    )
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("h3")))
    _answer_but_measurements(host)
    assert not [
        request
        for request in _issued(host, StartAttempt)
        if _line_of(request.decision_id.root).endswith(".h2")
        and _line_of(request.decision_id.root) != "000002.h2"
    ]

    # Once h2's round is in, nothing of it was dropped: it was measured and settled.
    _end_measurement(host, "000001.h1")
    host.drain()
    (h2,) = [item for item in _record(host).finished if item.sequence == 2]
    assert h2.failure is None
    assert "000002.h2" in _measured(host)
    queue = _record(host).evaluations
    assert queue is not None
    assert not queue.dropped
    assert isinstance(host.run.finished, Finish)
    assert_flow_closed(host)


@MODES
def test_a_wait_for_the_input_past_the_limit_reaches_the_orchestrators_next_turn(
    *, restarting: bool
) -> None:
    limit = 600.0
    cfg = config(
        max_rounds=2,
        max_in_flight=1,
        evaluation_slots=2,
        max_backlog=1,
        input_wait_seconds=limit,
    )
    host = begin(cfg, restarting=restarting, input_first=False)
    _plan(host, implement("h1"))
    _work(host, "000001.h1", "c1")

    # h1 holds the free evaluation slot, parked: its measurement waits for the input's, and the
    # planner is asked to fill its agent slot.
    assert host.input_waits()
    assert not _measured(host)
    first = host.run.expect_turn(role=PLANNER_LABEL)
    assert isinstance(first.context, PlannerPrompt)
    assert first.context.input_overdue is None

    # The limit passes: the run is not stuck silently, the orchestrator's next turn says so.
    host.run.advance_to(min(host.run.sleeping_until))
    record = _record(host)
    assert record.input_overdue is not None
    assert record.input_overdue.limit == limit
    host.run.reply_raw(first, "not json")
    again = host.run.expect_turn(role=PLANNER_LABEL).context
    assert isinstance(again, PlannerCorrection)
    assert again.input_overdue is not None
    assert again.input_overdue.limit_seconds == int(limit)
    assert again.input_overdue.waited_seconds is not None
    assert again.input_overdue.waited_seconds >= int(limit)
    assert "measurement is overdue" in render(PromptTemplate.PORTFOLIO_CORRECTION, again)

    # The orchestrator decides; once the input lands, the run ends as usual.
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("h2")))
    host.measure_input()
    host.drain()
    assert isinstance(host.run.finished, Finish)
    assert_flow_closed(host)


@MODES
def test_a_wait_for_the_input_past_the_limit_is_reported_while_the_input_holds_the_only_slot(
    *, restarting: bool
) -> None:
    """With one evaluation slot the input's measurement holds it, so no candidate is bound before
    the input lands: the overdue wait counts from the candidate's submission, not from a slot."""
    limit = 600.0
    cfg = config(
        max_rounds=2,
        max_in_flight=1,
        evaluation_slots=1,
        max_backlog=1,
        input_wait_seconds=limit,
    )
    host = begin(cfg, restarting=restarting, input_first=False)
    _plan(host, implement("h1"))
    _work(host, "000001.h1", "c1")

    # h1 is submitted but not bound: the input's measurement holds the only slot.
    assert host.input_waits()
    assert not _measuring(host)
    first = host.run.expect_turn(role=PLANNER_LABEL)
    assert isinstance(first.context, PlannerPrompt)
    assert first.context.input_overdue is None

    # The limit passes with the input still unmeasured: the orchestrator's next turn says so.
    host.run.advance_to(min(host.run.sleeping_until))
    record = _record(host)
    assert record.input_overdue is not None
    assert record.input_overdue.limit == limit
    host.run.reply_raw(first, "not json")
    again = host.run.expect_turn(role=PLANNER_LABEL).context
    assert isinstance(again, PlannerCorrection)
    assert again.input_overdue is not None
    assert again.input_overdue.limit_seconds == int(limit)
    assert again.input_overdue.waited_seconds is not None
    assert again.input_overdue.waited_seconds >= int(limit)
    assert "measurement is overdue" in render(PromptTemplate.PORTFOLIO_CORRECTION, again)

    # Once the input lands, h1 takes the slot and the run ends as usual.
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("h2")))
    host.measure_input()
    host.drain()
    assert isinstance(host.run.finished, Finish)
    assert_flow_closed(host)


@MODES
def test_a_step_accepted_on_its_own_checks_settles_before_the_input_lands(
    *, restarting: bool
) -> None:
    host = begin(plan_config(step("a")), restarting=restarting, input_first=False)
    _work(host, "000001.a", "a-rev")

    # The step is never measured, so nothing in it waits for the input's measurement.
    assert host.input_waits()
    (settled,) = _record(host).finished
    assert settled.plan.work_id == "a"
    assert settled.failure is None
    assert not _measured(host)

    host.measure_input()
    host.drain()
    assert isinstance(host.run.finished, Finish)
    assert_flow_closed(host)


@MODES
def test_a_stop_keeps_the_reviewed_candidates_that_were_never_measured(*, restarting: bool) -> None:
    """A stop (or the run's deadline) before a reviewed candidate is measured does not throw away
    its code: it settles unmeasured with the reason, kept as work in progress."""
    host = begin(config(max_rounds=3, **QUEUED), restarting=restarting)
    _plan(host, implement("h1"))
    _work(host, "000001.h1", "c1")
    _plan(host, implement("h2"))
    _work(host, "000002.h2", "c2")
    assert _measuring(host) == ["000001.h1"]  # h2 waits for the slot h1 holds

    host.run.stop()
    host.drain()

    (waited,) = [item for item in _record(host).finished if item.plan.work_id == "h2"]
    assert waited.failure is not None
    assert (waited.failure.kind, waited.failure.dropped) == ("not-measured", "stopped")
    (settled,) = [
        request.disposition
        for request in _issued(host, Withdraw)
        if _line_of(request.decision_id.root) == "000002.h2"
    ]
    assert isinstance(settled, Settle)
    assert settled.retention == "wip"
    assert settled.candidate == RevisionRef.of_git_commit("c2")
    assert not settled.eligible
    queue = _record(host).evaluations
    assert queue is not None
    assert [(entry.submission.work_id, why.reason) for entry, why in queue.dropped] == [
        ("h2", "stopped")
    ]
    assert _measured(host) == ["000001.h1"]
    assert isinstance(host.run.finished, Finish)
    assert_flow_closed(host)
