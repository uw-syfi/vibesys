"""A trusted measurement of the work an implementer abandoned is never lost (round 8 of a real run).

An implementer evaluated its candidate twice through the trusted evaluation tool (passing), then
replied `blocked` with `discard` because it could not meet a demand that was not a measurement. A
blocked reply produces no checkpoint, review is not due, and the round closed with no number at
all. The framework now reads the revision the agent evaluated with its own trusted gates (which
reuse the agent's runs) and records the reading and its comparison verdict with the round, marked
by the implementer's own outcome, without ever adopting that revision. These stories play the host
of `multi` and read what the search recorded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.multi._host import IMPLEMENTER, PLANNER, config, implemented
from tests.vibesys.serial_search._host import INPUT_THROUGHPUT
from tests.vibesys.vibesys_flows import across_restarts

from vs_core.api import FailureReason, Retained
from vs_sim.api.testing import examples

from .test_stories import design, implement, judge, selection_of, start

if TYPE_CHECKING:
    from tests.vibesys.orchestration.multi._host import MultiHost

    from vibesys.hypothesis.history import RoundRecord

EVALUATED = "e1"
RATIO = 1.5
NOISE = 0.01
STATUSES = ("blocked", "implementation_failed", "continue")


def played(
    *, restarting: bool, status: str | None, evaluated: bool, evaluations: int = 2, judged: bool
) -> tuple[MultiHost, RoundRecord]:
    """One round whose implementer replies ``status`` (None: never replies) after evaluating.

    The judge reviews the round when ``judged`` (it is the run's only round); otherwise the
    cadence defers the review (the first of three rounds, a judge every second) and the run is
    read when the next agent turn after it opens.
    """
    host = start(
        config(
            max_rounds=1 if judged else 3,
            judge_every=1 if judged else 2,
            max_retries_per_round=1,
        ),
        restarting=restarting,
        comparisons={EVALUATED: (RATIO, NOISE)},
    )
    design(host)
    host.drain_until_turn(IMPLEMENTER)
    for _ in range(evaluations if evaluated else 0):
        host.evaluate(EVALUATED)
    asked = host.run.expect_turn(role=IMPLEMENTER)
    if status is None:
        host.run.fail(asked, FailureReason.TIMED_OUT)
    elif status == "blocked":
        host.run.reply(asked, implemented("blocked", candidate_disposition="discard"))
    else:
        reply = implemented(status)
        host.run.reply(asked, reply, checkpoint=Retained(revision=host.retained()))
    if judged:
        host.drain()
    else:
        while not any(item.label in {PLANNER, IMPLEMENTER} for item in host.run.open):
            assert host.step(0), host.run.timeline()
    record = host.record
    assert record is not None
    return host, record.hypotheses[0].rounds[0]


@across_restarts
def test_a_blocked_round_the_cadence_did_not_review_keeps_the_measurement_it_evaluated(
    *, restarting: bool
) -> None:
    """Round 8: two passing evaluations, then `blocked` with `discard`, no checkpoint, no review."""
    host, row = played(restarting=restarting, status="blocked", evaluated=True, judged=False)

    assert host.measured == [EVALUATED]
    assert row.perf_metric == INPUT_THROUGHPUT * RATIO
    assert row.perf_provenance == "framework"
    assert row.perf_comparison is not None
    assert row.perf_comparison.value == "better"
    assert row.commit == EVALUATED
    assert row.hypothesis_outcome == "blocked"
    assert row.judge_verdict == "deferred"
    assert not row.passed
    assert row.candidate_retained is False
    assert row.candidate_disposition == "discard"


@across_restarts
def test_a_blocked_round_the_judge_reviewed_keeps_the_measurement_it_evaluated(
    *, restarting: bool
) -> None:
    host, row = played(restarting=restarting, status="blocked", evaluated=True, judged=True)

    assert host.measured == [EVALUATED]
    assert row.perf_metric == INPUT_THROUGHPUT * RATIO
    assert row.hypothesis_outcome == "blocked"
    assert row.candidate_retained is False
    assert selection_of(host) == ("trusted_baseline", None)


@across_restarts
def test_a_blocked_round_that_evaluated_nothing_records_no_measurement(*, restarting: bool) -> None:
    host, row = played(restarting=restarting, status="blocked", evaluated=False, judged=False)

    assert host.measured == []
    assert row.perf_metric is None
    assert row.perf_provenance is None
    assert row.hypothesis_outcome == "blocked"


@across_restarts
@settings(max_examples=examples(pr=16, nightly=48), deadline=None, derandomize=True, database=None)
@given(
    status=st.sampled_from((*STATUSES, None)),
    evaluated=st.booleans(),
    evaluations=st.integers(1, 3),
    judged=st.booleans(),
)
def test_a_trusted_measurement_is_never_dropped_and_never_recorded_without_a_trusted_gate(
    *, restarting: bool, status: str | None, evaluated: bool, evaluations: int, judged: bool
) -> None:
    host, row = played(
        restarting=restarting,
        status=status,
        evaluated=evaluated,
        evaluations=evaluations,
        judged=judged,
    )

    gated = host.measured == [EVALUATED]
    assert gated == evaluated, "the gates measure exactly what the agent evaluated"
    assert (row.perf_metric is not None) == evaluated
    if row.perf_metric is not None:
        assert row.perf_provenance == "framework"
        assert gated
    assert row.candidate_retained is not True
    if status is not None:
        assert row.hypothesis_outcome == status
    if judged:
        assert selection_of(host) == ("trusted_baseline", None)


@across_restarts
def test_an_implemented_round_is_measured_at_its_candidate_not_at_the_revision_evaluated(
    *, restarting: bool
) -> None:
    host = start(
        config(max_rounds=1),
        restarting=restarting,
        comparisons={"def456": (RATIO, NOISE), EVALUATED: (RATIO, NOISE)},
    )
    design(host)
    host.drain_until_turn(IMPLEMENTER)
    host.evaluate(EVALUATED)
    implement(host)
    judge(host, passed=True)
    host.drain()

    assert host.measured == ["def456"]
    assert selection_of(host) == ("retained_candidate", "def456")
