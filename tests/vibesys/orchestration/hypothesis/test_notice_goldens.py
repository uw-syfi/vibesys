"""Golden text for the hypothesis notices that feed agent prompts.

``pareto_archive_summary``, ``pareto_archive_conflict``,
``terminal_workspace_notice``, and the carry-over notices from
``HypothesisSearch.close_round`` are read by agents verbatim, so their exact bytes (including newlines) are pinned per branch. Regenerate with
``UPDATE_PROMPT_SNAPSHOTS=1`` when a wording change is intended.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vibesys.orchestration.hypothesis import (
    CarryOver,
    ExhaustionNotice,
    HypothesisConfig,
    HypothesisSearch,
    OrchestratorPlan,
    RegressionNotice,
)
from vibesys.orchestration.metrics import MetricComparison, MetricSpace, Objective
from vibesys.orchestration.multi import prompts as multi_prompts
from vibesys.orchestration.prompts import PROMPTS_DIR, render_template
from vibesys.orchestration.single import prompts as single_prompts
from vs_loop_state.api import (
    CandidateDisposition,
    HypothesisOutcome,
    PerfProvenance,
    RoundRecord,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_GOLDEN_DIR = Path(__file__).parent / "notice_goldens"
_SEARCH = HypothesisSearch(HypothesisConfig(max_rounds=10, max_retries_per_round=3))
_NOTICES = "_notices"
_SHARED_PROMPTS = PROMPTS_DIR / "shared"


def pareto_archive_summary(records: list[RoundRecord], space: MetricSpace) -> str:
    archive = _SEARCH.archive_view(records, space=space)
    return str(
        render_template(
            f"{_NOTICES}/pareto_archive.j2", template_dir=_SHARED_PROMPTS, archive=archive
        )
    )


def terminal_workspace_notice(records: list[RoundRecord]) -> str | None:
    notice = _SEARCH.initial_carry(records).regression
    return _regression_text(notice)


def _regression_text(notice: RegressionNotice | None) -> str | None:
    if notice is None:
        return None
    return str(
        render_template(
            f"{_NOTICES}/regression.j2", template_dir=_SHARED_PROMPTS, regression_info=notice
        )
    )


def _exhaustion_text(notice: ExhaustionNotice | None) -> str | None:
    if notice is None:
        return None
    return str(
        render_template(
            f"{_NOTICES}/exhaustion.j2", template_dir=_SHARED_PROMPTS, exhaustion_info=notice
        )
    )


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
    provenance: PerfProvenance | None = None,
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


def _terminal(
    outcome: HypothesisOutcome, number: int = 5, *, parent_round: int | None = None
) -> RoundRecord:
    return _row(number, outcome=outcome.value, hypothesis_id="h5", parent_round=parent_round)


def _checkpoint(number: int, outcome: str, *, reviewed: bool = True) -> RoundRecord:
    return _row(number, outcome=outcome, hypothesis_id=f"h{number}", reviewed=reviewed)


def _summary_inputs() -> dict[str, tuple[list[RoundRecord], MetricSpace]]:
    many_pending = [_pending(n, 1000.0 + n, 50.0 - n) for n in range(20, 31)]
    one_omitted = [_pending(n, 1000.0 + n, 50.0 - n) for n in range(20, 29)]
    two_omitted = [_pending(n, 1000.0 + n, 50.0 - n) for n in range(20, 30)]
    return {
        "summary_no_records_no_axes": ([], _NO_AXES),
        "summary_no_axes_with_latest": (
            [_row(1, perf=(12.5, "tok/s"), official=True, provenance="framework")],
            _NO_AXES,
        ),
        "summary_empty_archive": ([], _SPACE),
        "summary_single_objective": (
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
        "summary_dominated_and_non_dominated": (
            [
                _trusted(1, 1000.0, 90.0),
                _trusted(2, 2000.0, 100.0, point="c=128"),
                _trusted(3, 900.0, 95.0),
            ],
            _SPACE,
        ),
        "summary_latest_untrusted_unmeasured": ([_row(1, passed=False, reviewed=False)], _SPACE),
        "summary_latest_scalar_only": (
            [_row(1, perf=(33.0, "tok/s"), official=True, provenance="framework")],
            _SPACE,
        ),
        "summary_no_unit_scalar": (
            [
                _row(1, perf=(33.0, ""), official=True, provenance="framework"),
            ],
            _SPACE,
        ),
        "summary_pending_only": ([_pending(3, 6000.0, 3600.0)], _SPACE),
        "summary_trusted_and_pending": (
            [_trusted(1, 1000.0, 90.0), _pending(3, 6000.0, 3600.0)],
            _SPACE,
        ),
        "summary_pending_omitted_many": (many_pending, _SPACE),
        "summary_pending_omitted_one": (one_omitted, _SPACE),
        "summary_pending_omitted_two": (two_omitted, _SPACE),
    }


_SUMMARY_INPUTS = _summary_inputs()


def _summaries() -> dict[str, Callable[[], str]]:
    return {
        name: (lambda inputs=inputs: pareto_archive_summary(*inputs))
        for name, inputs in _SUMMARY_INPUTS.items()
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
                _row(
                    5,
                    outcome=HypothesisOutcome.DISPROVEN.value,
                    hypothesis_id="h5",
                    metrics={"throughput": 5.0},
                    retained=True,
                    passed=True,
                    reviewed=True,
                )
            ]
        ),
        "notice_retained_awaiting_review": lambda: terminal_workspace_notice(
            [
                _row(
                    5,
                    outcome=HypothesisOutcome.INCONCLUSIVE.value,
                    hypothesis_id="h5",
                    retained=True,
                    passed=False,
                    reviewed=False,
                )
            ]
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
            [_terminal(HypothesisOutcome.IMPLEMENTATION_FAILED, 1)]
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
            [*base, _terminal(HypothesisOutcome.DISPROVEN, 3, parent_round=1)]
        ),
        "notice_checkpoint_equals_parent": lambda: terminal_workspace_notice(
            [*base, _terminal(HypothesisOutcome.DISPROVEN, 3, parent_round=2)]
        ),
        "notice_checkpoint_reviewed": lambda: terminal_workspace_notice(
            [
                _checkpoint(1, "continue", reviewed=True),
                _terminal(HypothesisOutcome.BLOCKED, 3, parent_round=0),
            ]
        ),
        "notice_unspecified_hypothesis": lambda: terminal_workspace_notice(
            [_row(2, outcome="disproven")]
        ),
    }


@dataclass(frozen=True)
class _Closing:
    passed: bool
    reviewed: bool
    feedback: str | None = None
    terminal_needs_parent_choice: bool = False
    keeps_active: bool = False


def _close(record: RoundRecord, closing: _Closing) -> CarryOver:
    search = _SEARCH
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
        records=[],
    )
    closed = search.close_round(
        started.state,
        hypothesis=started.hypothesis,
        record=record,
        records=[],
        passed=closing.passed,
        reviewed=closing.reviewed,
        feedback=closing.feedback,
        keeps_active=closing.keeps_active,
        requests_continuation=False,
        next_step=None,
        terminal_needs_parent_choice=closing.terminal_needs_parent_choice,
    )
    return closed.carry


def _carry_text(carry: CarryOver) -> str:
    exhaustion = _exhaustion_text(carry.exhaustion)
    regression = _regression_text(carry.regression)
    return f"exhaustion_info={exhaustion!r}\nregression_info={regression!r}\n"


def _carries() -> dict[str, Callable[[], str]]:
    plain = _row(5, hypothesis_id="h5", outcome="supported")
    return {
        "carry_exhaustion_with_feedback": lambda: _carry_text(
            _close(plain, _Closing(passed=False, reviewed=True, feedback="needs tests"))
        ),
        "carry_exhaustion_empty_feedback": lambda: _carry_text(
            _close(plain, _Closing(passed=False, reviewed=True, feedback=None))
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
                _Closing(passed=True, reviewed=True),
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
                _Closing(passed=True, reviewed=True),
            )
        ),
        "carry_passed_terminal_parent_choice": lambda: _carry_text(
            _close(
                _terminal(HypothesisOutcome.DISPROVEN),
                _Closing(passed=True, reviewed=True, terminal_needs_parent_choice=True),
            )
        ),
        "carry_passed_clears_both": lambda: _carry_text(
            _close(plain, _Closing(passed=True, reviewed=True))
        ),
        "carry_unreviewed_terminal_notice": lambda: _carry_text(
            _close(
                _terminal(HypothesisOutcome.INCONCLUSIVE),
                _Closing(passed=False, reviewed=False),
            )
        ),
        "carry_unreviewed_keeps_active": lambda: _carry_text(
            _close(plain, _Closing(passed=False, reviewed=False, keeps_active=True))
        ),
    }


def _conflicts() -> dict[str, Callable[[], str | None]]:
    def conflict(
        records: list[RoundRecord],
        metrics: dict[str, float],
        disposition: CandidateDisposition = CandidateDisposition.PARETO_FRONTIER,
    ) -> str | None:
        found = _SEARCH.pareto_conflict(
            disposition=disposition, metrics=metrics, records=records, space=_SPACE
        )
        if found is None:
            return None
        return str(
            render_template(
                f"{_NOTICES}/archive_conflict.j2",
                template_dir=_SHARED_PROMPTS,
                pareto_archive_conflict=found,
            )
        )

    return {
        "conflict_one_dominator": lambda: conflict(
            [_trusted(61, 8795.8, 7724.0)], {"throughput": 7258.5, "latency": 9601.6}
        ),
        "conflict_two_dominators": lambda: conflict(
            [_trusted(3, 9000.0, 70.0), _trusted(4, 9500.0, 75.0)],
            {"throughput": 100.0, "latency": 100.0},
        ),
        "conflict_tradeoff_is_none": lambda: conflict(
            [_trusted(6, 100.0, 80.0)], {"throughput": 140.0, "latency": 100.0}
        ),
        "conflict_discard_is_none": lambda: conflict(
            [_trusted(61, 8795.8, 7724.0)],
            {"throughput": 7258.5, "latency": 9601.6},
            CandidateDisposition.DISCARD,
        ),
    }


_CASES: dict[str, Callable[[], str | None]] = {
    **_summaries(),
    **_conflicts(),
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


_PLUGIN_PROMPTS = {"multi": multi_prompts, "single": single_prompts}


@pytest.mark.parametrize("plugin", sorted(_PLUGIN_PROMPTS))
@pytest.mark.parametrize("name", sorted(_summaries()))
def test_pareto_document_wraps_the_golden_archive(plugin: str, name: str) -> None:
    """Each plugin's progress document is the archive under a fixed heading."""
    records, space = _SUMMARY_INPUTS[name]
    archive = _SEARCH.archive_view(records, space=space)
    document = _PLUGIN_PROMPTS[plugin].render_pareto_frontier(archive)
    golden = (_GOLDEN_DIR / f"{name}.txt").read_text(encoding="utf-8")
    assert document == f"# Pareto frontier\n\n{golden.rstrip()}\n"


@pytest.mark.parametrize("plugin", sorted(_PLUGIN_PROMPTS))
def test_pareto_guard_appends_the_golden_conflict_to_the_review(plugin: str) -> None:
    """A guarded pass keeps the agent's review verbatim and cites the conflict."""
    conflict = _SEARCH.pareto_conflict(
        disposition=CandidateDisposition.PARETO_FRONTIER,
        metrics={"throughput": 7258.5, "latency": 9601.6},
        records=[_trusted(61, 8795.8, 7724.0)],
        space=_SPACE,
    )
    assert conflict is not None
    golden = (_GOLDEN_DIR / "conflict_one_dominator.txt").read_text(encoding="utf-8")
    prompts = _PLUGIN_PROMPTS[plugin]
    review = "  Looks right.\n"
    assert prompts.render_archive_conflict(conflict) == golden
    assert (
        prompts.render_pareto_guard(review, conflict)
        == f"{review}\n\nFramework Pareto guard: {golden}"
    )
