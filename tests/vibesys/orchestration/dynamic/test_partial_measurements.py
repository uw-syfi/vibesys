"""A candidate whose benchmark fails after part of its warmup is told to the planner by how far it got.

Regression for r14 (live run): every candidate failed the benchmark, the planner saw no number
for any of them and could not tell which came closest. The evaluator now reports what the failed
benchmark measured; the search keeps it on the round, lists the candidates the planner may build on
best first (a failed benchmark by how far it got), and shows each its partial measurement.
The wording of those rows is the prompt templates' business (`core_policy`); the ranking among
parents is the catalog's (`test_parent_catalog.py`). What is asserted here is the join: a
benchmark that failed with a partial measurement reaches the planner of the next plan.
"""

from __future__ import annotations

from typing import Literal

from tests.vibesys.orchestration.dynamic._host import FACTS, PLANNER_LABEL, Host, config
from tests.vibesys.orchestration.dynamic._replies import implement, implemented, plan_reply
from tests.vibesys.orchestration.dynamic._slots import (
    implement_and_judge,
    open_next_slot,
    planner_context,
    turn_about,
)
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.metrics import EvidenceReading, PartialRow
from vibesys.orchestration.dynamic.models import ImplementerResult
from vibesys.orchestration.dynamic.search.api import (
    DynamicConfig,
    PlannerPrompt,
    ProfileHistoryRow,
    dynamic,
)
from vs_core.api import EvidenceKind, EvidenceRef, Finish, Measure, ObservationStatus
from vs_core.api.testing import Asked, FlowRun

REQUIRED = 72
MODES = across_restarts


def reached(rounds: int) -> PartialRow:
    """What a warmup that stopped after ``rounds`` of the required rounds had measured."""
    return PartialRow(
        name="warmup_rounds_per_s",
        value=float(rounds),
        direction="max",
        unit="rounds/s",
        target=float(REQUIRED),
        completed=rounds,
        required=REQUIRED,
        progress_unit="rounds",
    )


class WarmupHost(Host):
    """A world in which the named candidates miss the benchmark's warmup after some rounds."""

    def __init__(
        self,
        run: FlowRun[DynamicConfig],
        cfg: DynamicConfig,
        reached_rounds: dict[str, int],
        baseline: Literal["measured", "failed-workload"],
    ) -> None:
        """Play ``run`` as the ordinary world, except for the candidates in ``reached_rounds``."""
        super().__init__(run, cfg, baseline=baseline)
        self.reached_rounds = reached_rounds
        self._measuring = ""

    def _measure(self, asked: Asked, request: Measure) -> None:
        self._measuring = request.plan.candidate.revision_id.root  # ty: ignore[unresolved-attribute]
        super()._measure(asked, request)

    def _status_of(self, stage: str, *, is_input: bool) -> ObservationStatus:
        missed = stage == "benchmark" and self._measuring in self.reached_rounds
        return ObservationStatus.FAILED if missed else super()._status_of(stage, is_input=is_input)

    def _reading_of(self, ref: EvidenceRef) -> EvidenceReading:
        reading = super()._reading_of(ref)
        rounds = self.reached_rounds.get(ref.candidate.revision_id.root)
        if ref.kind is not EvidenceKind.BENCHMARK or rounds is None:
            return reading
        return reading.model_copy(update={"passed": False, "partial": reached(rounds)})


def start(
    *,
    restarting: bool,
    reached_rounds: dict[str, int],
    baseline: Literal["measured", "failed-workload"] = "measured",
) -> WarmupHost:
    """Run the input's measurement up to the first planner turn."""
    cfg = config(max_rounds=3, max_in_flight=1)
    run = FlowRun.start(dynamic, cfg, facts=FACTS, restarting=restarting)
    host = WarmupHost(run, cfg, reached_rounds, baseline)
    host.measure_input()
    host.drain_until_turn(PLANNER_LABEL)
    return host


def finish_workstream(host: Host, hypothesis: str, candidate: str) -> None:
    open_next_slot(host)
    implement_and_judge(host, hypothesis, candidate)
    for asked in host.open(Measure):
        host.answer(asked)
    host.drain_until_turn(PLANNER_LABEL)


def planned(host: Host) -> PlannerPrompt:
    context = planner_context(host)
    assert isinstance(context, PlannerPrompt)
    return context


def partials(context: PlannerPrompt) -> dict[str, PartialRow | None]:
    """The partial measurement the planner is shown for each hypothesis of its history."""
    return {
        row.hypothesis_id: row.partial
        for row in context.history
        if not isinstance(row, ProfileHistoryRow)
    }


@MODES
def test_the_planner_is_shown_each_failed_benchmark_by_how_far_it_got_closest_first(
    *, restarting: bool
) -> None:
    host = start(restarting=restarting, reached_rounds={"c1": 14, "c2": 38})
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("A")))
    finish_workstream(host, "A", "c1")
    after_first = planned(host)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("B")))
    finish_workstream(host, "B", "c2")
    after_second = planned(host)

    # Only what has been measured so far is shown: the first plan follows one candidate.
    assert partials(after_first) == {"A": reached(14)}
    assert [(row.hypothesis_id, row.partial) for row in after_first.buildable] == [
        ("A", reached(14))
    ]
    # Both are failed benchmarks, none is a trusted result, and the closer one is listed first.
    assert partials(after_second) == {"A": reached(14), "B": reached(38)}
    assert [row.hypothesis_id for row in after_second.buildable] == ["B", "A"]
    assert [row.best_partial for row in after_second.buildable] == [True, False]
    assert all(row.metric is None for row in after_second.buildable)


@MODES
def test_no_candidate_is_adopted_for_a_benchmark_it_failed_however_far_it_got(
    *, restarting: bool
) -> None:
    host = start(
        restarting=restarting,
        reached_rounds={"c1": 14, "c2": 38},
        baseline="failed-workload",
    )
    # The first plan was made while the input was being measured; the input missed the bar too,
    # so that plan is dropped and the planner is told so rather than shown a baseline.
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("A")))
    for hypothesis, candidate in (("A", "c1"), ("B", "c2")):
        told = planned(host)
        assert told.input_failure is not None
        assert told.baseline == ()
        host.run.reply_raw(
            host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement(hypothesis))
        )
        finish_workstream(host, hypothesis, candidate)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("C")))
    open_next_slot(host)
    blocked = ImplementerResult.model_validate_json(implemented("blocked"))
    host.run.reply(turn_about(host, "implementer", "C"), blocked)
    host.drain()

    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.outcome == "failure"
    assert finish.result.selection is None
    assert finish.result.reason.startswith("no-winner: no trusted result")
    assert not host.unsettled
