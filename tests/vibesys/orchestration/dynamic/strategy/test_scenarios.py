"""Whole runs on the production shell: baseline, plan, implement, review, measure, select, adopt.

Each run goes through `vs_runtime`'s real run loop and shell (`_shell.drive_shell`), with
only the executors scripted, so what core is handed is what production hands it.
"""

from __future__ import annotations

from collections import deque

from tests.vibesys.orchestration.dynamic.strategy._executors import Executors
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import kinds, run_shell

from vs_core.api import Operation, ProposeWinner, RequestTurn, StartAttempt, Stop


def _one_hypothesis() -> Executors:
    return Executors(
        planner=deque([plan_reply(implement("h1"))]),
        implementer=deque([implemented()]),
        judge=deque([reviewed()]),
    )


def test_single_hypothesis_runs_to_adoption() -> None:
    trace = run_shell(_one_hypothesis())
    assert kinds(trace).count("StartAttempt") == 1
    final = trace.decisions[-1]
    assert isinstance(final, Stop)
    assert final.result.outcome == "success"
    proposal = next(item for item in trace.decisions if isinstance(item, ProposeWinner))
    assert proposal.selection.kind == "retained_candidate"


def test_invalid_plan_gets_one_correction_turn() -> None:
    """A malformed plan is corrected once, then accepted."""
    executors = _one_hypothesis()
    executors.planner.appendleft("not json")
    trace = run_shell(executors)
    planner_turns = [
        item
        for item in trace.decisions
        if isinstance(item, RequestTurn) and item.turn.session.role_id.root.endswith("orchestrator")
    ]
    assert len(planner_turns) == 2
    assert kinds(trace).count("StartAttempt") == 1
    assert isinstance(trace.decisions[-1], Stop)


def test_best_of_two_measured_candidates_is_proposed() -> None:
    """The strongest eligible candidate wins over a weaker one."""
    executors = Executors(
        planner=deque([plan_reply(implement("h1"), implement("h2"))]),
        implementer=deque([implemented(), implemented()]),
        judge=deque([reviewed(), reviewed()]),
    )
    trace = run_shell(executors, max_in_flight=2)
    proposal = next(item for item in trace.decisions if isinstance(item, ProposeWinner))
    assert proposal.selection.kind == "retained_candidate"


def test_no_improvement_selects_the_trusted_baseline() -> None:
    """A candidate that does not beat the baseline is not adopted."""
    executors = _one_hypothesis()
    executors.benchmark = lambda _commit: 10.0
    trace = run_shell(executors)
    proposal = next(item for item in trace.decisions if isinstance(item, ProposeWinner))
    assert proposal.selection.kind == "trusted_baseline"


def test_failed_review_makes_a_candidate_ineligible() -> None:
    """A judge verdict of not passed keeps the candidate from adoption."""
    executors = _one_hypothesis()
    executors.judge = deque([reviewed(passed=False)])
    trace = run_shell(executors)
    proposal = next(item for item in trace.decisions if isinstance(item, ProposeWinner))
    assert proposal.selection.kind == "trusted_baseline"


def test_every_attempt_is_started_from_an_operation_rendered_prompt() -> None:
    trace = run_shell(_one_hypothesis())
    first_attempt = next(
        index for index, item in enumerate(trace.decisions) if isinstance(item, StartAttempt)
    )
    assert any(isinstance(item, Operation) for item in trace.decisions[:first_attempt])


def test_a_correction_turn_speaks_in_its_predecessors_session() -> None:
    """Core requires every turn of one session to carry one spec, corrections included."""
    executors = _one_hypothesis()
    executors.planner.appendleft("not json")
    trace = run_shell(executors)
    assert isinstance(trace.decisions[-1], Stop)
    turns = [item.turn for item in trace.decisions if isinstance(item, RequestTurn)]
    corrections = [turn for turn in turns if turn.charge_class == "correction"]
    assert len(corrections) == 1
    assert corrections[0].predecessor is not None
    for turn in turns:
        same_session = {t.session for t in turns if t.session.session_id == turn.session.session_id}
        assert same_session == {turn.session}
