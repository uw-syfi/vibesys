"""Golden text for the hypothesis notices that feed agent prompts.

``pareto_archive_summary``, ``terminal_workspace_notice``, and the carry-over
notices from ``HypothesisSearch.close_round`` are read by agents verbatim, so
their exact bytes (including newlines) are pinned per branch. Regenerate with
``UPDATE_PROMPT_SNAPSHOTS=1`` when a wording change is intended.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from vibesys.orchestration.hypothesis import (
    CarryOver,
    HypothesisConfig,
    HypothesisSearch,
    OrchestratorPlan,
)
from vibesys.orchestration.hypothesis.transitions import (
    pareto_archive_summary,
    terminal_workspace_notice,
)
from vibesys.orchestration.metrics import MetricComparison, MetricSpace, Objective
from vs_loop_state.api import CandidateDisposition, HypothesisOutcome, RoundRecord

_GOLDEN_DIR = Path(__file__).parent / "notice_goldens"

_SPACE = MetricSpace(
    relative_noise=0.02,
    objectives=(
        Objective(name="throughput", direction="max"),
        Objective(name="latency", direction="min"),
    ),
)
_SINGLE_AXIS = MetricSpace(objectives=(Objective(name="throughput", direction="max"),))
_NO_AXES = MetricSpace(objectives=())


def _row(  # noqa: PLR0913  # LW-040137 [PLR0913]; the keyword-only fields are independent record attributes that a golden case sets directly.
    number: int,
    *,
    commit: str | None = None,
    metrics: dict[str, float] | None = None,
    perf: tuple[float, str | None] | None = None,
    official: bool = False,
    provenance: str | None = None,
    passed: bool = True,
    reviewed: bool = True,
    outcome: str | None = None,
    retained: bool | None = None,
    hypothesis_id: str | None = None,
    parent_round: int | None = None,
    operating_point: str = "",
    artifact: str | None = None,
    reason: str = "",
) -> RoundRecord:
    return RoundRecord(
        number,
        commit if commit is not None else f"{number:02d}" * 20,
        perf[0] if perf else None,
        perf[1] if perf else None,
        passed=passed,
        judge_verdict=("deferred" if not reviewed else "pass" if passed else "fail"),
        hypothesis_id=hypothesis_id,
        hypothesis_outcome=outcome,
        hypothesis_parent_round=parent_round,
        official_evaluation=official,
        perf_provenance=provenance,
        perf_comparison=(
            MetricComparison.INCOMPARABLE if official and provenance == "framework" else None
        ),
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_metrics=metrics or {},
        candidate_evaluation_artifact=artifact,
        candidate_operating_point=operating_point,
        candidate_retention_reason=reason,
        candidate_retained=retained,
        metrics=metrics or {},
    )


def _trusted(number: int, tput: float, lat: float, *, point: str = "c=64") -> RoundRecord:
    return _row(
        number,
        metrics={"throughput": tput, "latency": lat},
        perf=(tput, "tok/s"),
        official=True,
        provenance="framework",
        retained=True,
        operating_point=point,
        artifact=f"r{number}.json",
    )


def _pending(number: int, tput: float, lat: float) -> RoundRecord:
    return _row(
        number,
        metrics={"throughput": tput, "latency": lat},
        passed=False,
        reviewed=False,
        retained=True,
        operating_point=f"c={number}",
        artifact=f"p{number}.json",
        reason="tradeoff",
    )


def _terminal(outcome: HypothesisOutcome, **fields: object) -> RoundRecord:
    return _row(
        fields.pop("number", 5),  # type: ignore[arg-type]
        outcome=outcome.value,
        hypothesis_id=fields.pop("hypothesis_id", "h5"),  # type: ignore[arg-type]
        **fields,  # type: ignore[arg-type]
    )


def _checkpoint(number: int, outcome: str, *, reviewed: bool = True) -> RoundRecord:
    return _row(number, outcome=outcome, hypothesis_id=f"h{number}", reviewed=reviewed)


def _summaries() -> dict[str, Callable[[], str]]:
    many_pending = [_pending(n, 1000.0 + n, 50.0 - n) for n in range(20, 31)]
    one_omitted = [_pending(n, 1000.0 + n, 50.0 - n) for n in range(20, 29)]
    two_omitted = [_pending(n, 1000.0 + n, 50.0 - n) for n in range(20, 30)]
    return {
        "summary_no_records_no_axes": lambda: pareto_archive_summary([], _NO_AXES),
        "summary_no_axes_with_latest": lambda: pareto_archive_summary(
            [_row(1, perf=(12.5, "tok/s"), official=True, provenance="framework")], _NO_AXES
        ),
        "summary_empty_archive": lambda: pareto_archive_summary([], _SPACE),
        "summary_single_objective": lambda: pareto_archive_summary(
            [
                _row(
                    1,
                    metrics={"throughput": 10.0},
                    perf=(10.0, "tok/s"),
                    official=True,
                    provenance="framework",
                    retained=True,
                    artifact="a.json",
                ),
                _row(
                    2,
                    metrics={"throughput": 12.0},
                    perf=(12.0, "tok/s"),
                    official=True,
                    provenance="framework",
                    retained=True,
                ),
            ],
            _SINGLE_AXIS,
        ),
        "summary_dominated_and_non_dominated": lambda: pareto_archive_summary(
            [
                _trusted(1, 1000.0, 90.0),
                _trusted(2, 2000.0, 100.0, point="c=128"),
                _trusted(3, 900.0, 95.0),
            ],
            _SPACE,
        ),
        "summary_latest_untrusted_unmeasured": lambda: pareto_archive_summary(
            [_row(1, passed=False, reviewed=False)], _SPACE
        ),
        "summary_latest_scalar_only": lambda: pareto_archive_summary(
            [_row(1, perf=(33.0, "tok/s"), official=True, provenance="framework")], _SPACE
        ),
        "summary_no_unit_scalar": lambda: pareto_archive_summary(
            [
                _row(1, perf=(33.0, ""), official=True, provenance="framework"),
            ],
            _SPACE,
        ),
        "summary_pending_only": lambda: pareto_archive_summary(
            [_pending(3, 6000.0, 3600.0)], _SPACE
        ),
        "summary_trusted_and_pending": lambda: pareto_archive_summary(
            [_trusted(1, 1000.0, 90.0), _pending(3, 6000.0, 3600.0)], _SPACE
        ),
        "summary_pending_omitted_many": lambda: pareto_archive_summary(many_pending, _SPACE),
        "summary_pending_omitted_one": lambda: pareto_archive_summary(one_omitted, _SPACE),
        "summary_pending_omitted_two": lambda: pareto_archive_summary(two_omitted, _SPACE),
    }


def _notices() -> dict[str, Callable[[], str | None]]:
    base = [_checkpoint(1, "continue"), _checkpoint(2, "proven", reviewed=False)]
    return {
        "notice_no_records": lambda: terminal_workspace_notice([]),
        "notice_non_terminal_is_none": lambda: terminal_workspace_notice(
            [_terminal(HypothesisOutcome.CONTINUE)]
        ),
        "notice_retained_reviewed": lambda: terminal_workspace_notice(
            [
                _terminal(
                    HypothesisOutcome.DISPROVEN,
                    metrics={"throughput": 5.0},
                    retained=True,
                    passed=True,
                    reviewed=True,
                )
            ]
        ),
        "notice_retained_awaiting_review": lambda: terminal_workspace_notice(
            [_terminal(HypothesisOutcome.INCONCLUSIVE, retained=True, passed=False, reviewed=False)]
        ),
        "notice_retained_missing_commit": lambda: terminal_workspace_notice(
            [
                RoundRecord(
                    5,
                    None,
                    None,
                    None,
                    passed=True,
                    hypothesis_outcome="blocked",
                    candidate_retained=True,
                )
            ]
        ),
        "notice_first_round_no_parent": lambda: terminal_workspace_notice(
            [_terminal(HypothesisOutcome.IMPLEMENTATION_FAILED, number=1)]
        ),
        "notice_explicit_parent": lambda: terminal_workspace_notice(
            [_terminal(HypothesisOutcome.DISPROVEN, parent_round=3)]
        ),
        "notice_campaign_started_later": lambda: terminal_workspace_notice(
            [
                _checkpoint(1, "continue"),
                _row(4, outcome="continue", hypothesis_id="h5"),
                _terminal(HypothesisOutcome.DISPROVEN),
            ]
        ),
        "notice_with_checkpoint_guidance": lambda: terminal_workspace_notice(
            [*base, _terminal(HypothesisOutcome.DISPROVEN, number=3, parent_round=1)]
        ),
        "notice_checkpoint_equals_parent": lambda: terminal_workspace_notice(
            [*base, _terminal(HypothesisOutcome.DISPROVEN, number=3, parent_round=2)]
        ),
        "notice_checkpoint_reviewed": lambda: terminal_workspace_notice(
            [
                _checkpoint(1, "continue", reviewed=True),
                _terminal(HypothesisOutcome.BLOCKED, number=3, parent_round=0),
            ]
        ),
        "notice_unspecified_hypothesis": lambda: terminal_workspace_notice(
            [_row(2, outcome="disproven")]
        ),
    }


def _close(
    record: RoundRecord,
    *,
    passed: bool,
    reviewed: bool,
    feedback: str | None = None,
    terminal_needs_parent_choice: bool = False,
    keeps_active: bool = False,
    prior: CarryOver | None = None,
    earlier: list[RoundRecord] | None = None,
) -> CarryOver:
    search = HypothesisSearch(HypothesisConfig(max_rounds=10, max_retries_per_round=3))
    plan = OrchestratorPlan(
        hypothesis_id="h5",
        hypothesis="claim",
        task="task",
        pass_criteria="tests pass",  # noqa: S106  # LW-040073 [S106]; the argument is a fixture literal, not a credential.
        reasoning="why",
    )
    started = search.start(
        search.initial(),
        plan,
        round_number=record.round_number,
        current_commit=None,
        records=earlier or [],
    )
    closed = search.close_round(
        started.state,
        hypothesis=started.hypothesis,
        record=record,
        records=earlier or [],
        carry=prior or CarryOver(),
        passed=passed,
        reviewed=reviewed,
        feedback=feedback,
        keeps_active=keeps_active,
        requests_continuation=False,
        next_step=None,
        terminal_needs_parent_choice=terminal_needs_parent_choice,
    )
    return closed.carry


def _carry_text(carry: CarryOver) -> str:
    return f"exhaustion_info={carry.exhaustion_info!r}\nregression_info={carry.regression_info!r}\n"


def _carries() -> dict[str, Callable[[], str]]:
    plain = _row(5, hypothesis_id="h5", outcome="supported")
    stale = CarryOver(regression_info="old regression", exhaustion_info="old exhaustion")
    return {
        "carry_exhaustion_with_feedback": lambda: _carry_text(
            _close(plain, passed=False, reviewed=True, feedback="needs tests")
        ),
        "carry_exhaustion_empty_feedback": lambda: _carry_text(
            _close(plain, passed=False, reviewed=True, feedback=None, prior=stale)
        ),
        "carry_not_retained_with_unit": lambda: _carry_text(
            _close(
                _row(
                    5,
                    perf=(8.5, "tok/s"),
                    official=True,
                    provenance="framework",
                    retained=False,
                    hypothesis_id="h5",
                ),
                passed=True,
                reviewed=True,
                prior=stale,
            )
        ),
        "carry_not_retained_without_unit": lambda: _carry_text(
            _close(
                _row(
                    6,
                    perf=(8.5, None),
                    official=True,
                    provenance="framework",
                    retained=False,
                    hypothesis_id="h5",
                ),
                passed=True,
                reviewed=True,
            )
        ),
        "carry_passed_terminal_parent_choice": lambda: _carry_text(
            _close(
                _terminal(HypothesisOutcome.DISPROVEN),
                passed=True,
                reviewed=True,
                terminal_needs_parent_choice=True,
            )
        ),
        "carry_passed_clears_both": lambda: _carry_text(
            _close(plain, passed=True, reviewed=True, prior=stale)
        ),
        "carry_unreviewed_terminal_notice": lambda: _carry_text(
            _close(
                _terminal(HypothesisOutcome.INCONCLUSIVE), passed=False, reviewed=False, prior=stale
            )
        ),
        "carry_unreviewed_keeps_active": lambda: _carry_text(
            _close(plain, passed=False, reviewed=False, keeps_active=True, prior=stale)
        ),
    }


_CASES: dict[str, Callable[[], str | None]] = {
    **_summaries(),
    **_notices(),
    **_carries(),
}


@pytest.mark.parametrize("name", sorted(_CASES))
def test_notice_matches_golden_text(name: str) -> None:
    produced = _CASES[name]()
    rendered = "<None>" if produced is None else produced
    golden = _GOLDEN_DIR / f"{name}.txt"
    if os.environ.get("UPDATE_PROMPT_SNAPSHOTS") == "1":
        golden.write_text(rendered, encoding="utf-8")
        return
    assert rendered == golden.read_text(encoding="utf-8")
