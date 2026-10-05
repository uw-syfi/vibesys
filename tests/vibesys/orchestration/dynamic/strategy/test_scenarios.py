"""Whole runs on the real core: baseline, plan, implement, review, measure, select, adopt.

Kernel gaps keep these runs from finishing today, so every test here is a strict
xfail that flips to a failure the day the gaps close (remove the mark then).
Owners: the observe cycle is EVAL-PATH, the session reply proof is SESSION-WIRING,
retention is CORE-P2:

- A measurement cannot complete: core never issues `ObserveOwnedJob` after a
  submission is accepted, and accepts a later `JobObserved` only when it equals the
  submission request's own first observation (`vs_core/_measurements.py`,
  `_submission_job`, `_source`).
- Core refuses a session reply: a `RequestObserved` carrying `outcome_json` without a
  registered-codec proof is rejected (`vs_core/_step.py`, `_validate_observation_ingress`),
  yet `vs_runtime/_session_requests.py` builds exactly that observation.
- Nothing retains a candidate checkpoint after an implementer turn:
  `vs_core/_attempt_acquisition.py` (`_checkpoint_request`) raises
  `KernelNotImplementedError` for any retention but "wip", and no runtime executor
  handles `CloseAttemptScope`. With the first gap patched out, a run reaches the
  implementer turn and then settles the attempt as failed, "retained no candidate".
"""

from __future__ import annotations

from collections import deque

import pytest
from tests.vibesys.orchestration.dynamic.strategy._executors import Executors
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import kinds, run

from vs_core.api import Operation, ProposeWinner, RequestTurn, StartAttempt, Stop

pending_kernel = pytest.mark.xfail(
    strict=True,
    reason=(
        "run stops at the baseline Measure: no ObserveOwnedJob after a submission, "
        "libs/vs-core/src/vs_core/_measurements.py:452 (_source) and :571 (_submission_job), "
        "owner EVAL-PATH (#1333). Behind it: session reply proof, "
        "libs/vs-core/src/vs_core/_step.py:1187 vs libs/vs-runtime/src/vs_runtime/_session_requests.py:330, "
        "owner SESSION-WIRING; non-wip retention, "
        "libs/vs-core/src/vs_core/_attempt_acquisition.py:1161 and :1196, owner CORE-P2"
    ),
)


def _one_hypothesis() -> Executors:
    return Executors(
        planner=deque([plan_reply(implement("h1"))]),
        implementer=deque([implemented()]),
        judge=deque([reviewed()]),
    )


@pending_kernel
def test_single_hypothesis_runs_to_adoption() -> None:
    trace = run(_one_hypothesis())
    assert kinds(trace).count("StartAttempt") == 1
    final = trace.decisions[-1]
    assert isinstance(final, Stop)
    assert final.result.outcome == "success"
    proposal = next(item for item in trace.decisions if isinstance(item, ProposeWinner))
    assert proposal.selection.kind == "retained_candidate"


@pending_kernel
def test_invalid_plan_gets_one_correction_turn() -> None:
    """A malformed plan is corrected once, then accepted."""
    executors = _one_hypothesis()
    executors.planner.appendleft("not json")
    trace = run(executors)
    planner_turns = [
        item
        for item in trace.decisions
        if isinstance(item, RequestTurn) and item.turn.session.role_id.root.endswith("orchestrator")
    ]
    assert len(planner_turns) == 2
    assert kinds(trace).count("StartAttempt") == 1
    assert isinstance(trace.decisions[-1], Stop)


@pending_kernel
def test_best_of_two_measured_candidates_is_proposed() -> None:
    """The strongest eligible candidate wins over a weaker one."""
    executors = Executors(
        planner=deque([plan_reply(implement("h1"), implement("h2"))]),
        implementer=deque([implemented(), implemented()]),
        judge=deque([reviewed(), reviewed()]),
    )
    trace = run(executors, max_in_flight=2)
    proposal = next(item for item in trace.decisions if isinstance(item, ProposeWinner))
    assert proposal.selection.kind == "retained_candidate"


@pending_kernel
def test_no_improvement_selects_the_trusted_baseline() -> None:
    """A candidate that does not beat the baseline is not adopted."""
    executors = _one_hypothesis()
    executors.benchmark = lambda _commit: 10.0
    trace = run(executors)
    proposal = next(item for item in trace.decisions if isinstance(item, ProposeWinner))
    assert proposal.selection.kind == "trusted_baseline"


@pending_kernel
def test_failed_review_makes_a_candidate_ineligible() -> None:
    """A judge verdict of not passed keeps the candidate from adoption."""
    executors = _one_hypothesis()
    executors.judge = deque([reviewed(passed=False)])
    trace = run(executors)
    proposal = next(item for item in trace.decisions if isinstance(item, ProposeWinner))
    assert proposal.selection.kind == "trusted_baseline"


@pending_kernel
def test_every_attempt_is_started_from_an_operation_rendered_prompt() -> None:
    trace = run(_one_hypothesis())
    first_attempt = next(
        index for index, item in enumerate(trace.decisions) if isinstance(item, StartAttempt)
    )
    assert any(isinstance(item, Operation) for item in trace.decisions[:first_attempt])
