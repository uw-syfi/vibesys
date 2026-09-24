"""Failure-path tests for the evolutionary loop's cleanup and reporting helpers.

These drive the loop's private helpers with mocked contexts so each error
branch (setup, evaluation, teardown, bootstrap snapshot, abort reporting) is
checked without building a real run context or agent runner.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from vibesys.events import FrameworkWarningData
from vibesys.loops.evolve import loop as subject
from vibesys.loops.evolve.population import Individual, Population
from vibesys.loops.evolve.search_policy import SearchSelection, VibeSysSearchPolicy
from vibesys.loops.metrics import MetricSpace, Objective
from vibesys.render.sink import output_sink
from vs_agent.api import CandidateProgress

if TYPE_CHECKING:
    from collections.abc import Iterator

_Dynamic = Any


def _private(name: str) -> _Dynamic:
    """Return one private loop helper the tests deliberately drive."""
    return getattr(subject, name)


class _Warnings:
    def __init__(self) -> None:
        self.events: list[object] = []

    @property
    def summaries(self) -> list[str]:
        return [
            f"{event.data.summary} | {event.data.detail or ''}"
            for event in self.events
            if isinstance(getattr(event, "data", None), FrameworkWarningData)
        ]


@pytest.fixture
def warnings() -> Iterator[_Warnings]:
    """Collect the framework warnings emitted while a test runs."""
    collected = _Warnings()
    unsubscribe = output_sink().subscribe(collected.events.append)
    try:
        yield collected
    finally:
        unsubscribe()


def _individual(id_: int, *, commit: str | None = "abc123def", passed: bool = True) -> Individual:
    return Individual(
        id=id_, generation=1, parent_id=None, commit=commit, passed=passed, perf_metric=float(id_)
    )


def test_materialize_selected_candidate_requires_a_commit() -> None:
    ctx = MagicMock()

    with pytest.raises(RuntimeError, match="selected individual 4 has no Git commit"):
        _private("_materialize_selected_candidate")(ctx, _individual(4, commit=None))

    ctx.git.retain_candidate.assert_not_called()
    ctx.snapshot_workspace.assert_not_called()


def test_materialize_selected_candidate_reports_checkout_failure_and_success() -> None:
    ctx = MagicMock()
    ctx.git.checkout_tree.return_value = False

    with pytest.raises(RuntimeError, match=r"could not materialize selected individual 5 at abc"):
        _private("_materialize_selected_candidate")(ctx, _individual(5, commit="abc"))
    ctx.snapshot_workspace.assert_not_called()

    ctx.git.checkout_tree.return_value = True
    _private("_materialize_selected_candidate")(ctx, _individual(5, commit="abc"))
    ctx.git.retain_candidate.assert_called_with("selected-5", "abc")
    ctx.snapshot_workspace.assert_called_once_with("evolve: select individual 5")


def test_discard_working_tree_warns_when_checkout_fails_or_raises(warnings: _Warnings) -> None:
    ctx = MagicMock()
    ctx.git.checkout_tree.return_value = False
    _private("_discard_working_tree")(ctx)
    ctx.git.checkout_tree.side_effect = OSError("index locked")
    _private("_discard_working_tree")(ctx)

    assert warnings.summaries == [
        "discard working tree failed | ",
        "discard working tree failed | index locked",
    ]


def _search(population: Population | None = None, **request: object) -> _Dynamic:
    request_fields = {"config": None, "agent_backend": "cli", "cli_provider": None, **request}
    return _private("_EvolutionSearchSession")(
        request=SimpleNamespace(**request_fields),
        pass_criteria="",
        population=population if population is not None else Population([]),
        rng=SimpleNamespace(),
        search_policy=VibeSysSearchPolicy(),
    )


def _evaluate(parent_ctx: MagicMock) -> _Dynamic:
    return _private("_evaluate_in_subcontext")(
        parent_ctx,
        search=_search(),
        progress=CandidateProgress(2, 3, 1, 1),
        selection=SearchSelection(parent=_individual(1, commit="parentsha"), inspirations=[]),
    )


def test_candidate_setup_failure_becomes_a_failed_outcome(warnings: _Warnings) -> None:
    with patch.object(subject, "create_candidate_context", side_effect=OSError("no worktree")):
        outcome = _evaluate(MagicMock())

    assert outcome.passed is False
    assert outcome.parent_id == 1
    assert outcome.summary == "candidate setup failed"
    assert outcome.feedback == "no worktree"
    assert warnings.summaries == ["candidate g2c1 setup failed | no worktree"]


def test_candidate_evaluation_failure_is_folded_into_outcome_and_context_closed(
    warnings: _Warnings,
) -> None:
    subctx = MagicMock()
    with (
        patch.object(subject, "create_candidate_context", return_value=subctx),
        patch.object(subject, "_evaluate_candidate", side_effect=ValueError("judge exploded")),
    ):
        outcome = _evaluate(MagicMock())

    assert (outcome.passed, outcome.summary) == (False, "candidate evaluation raised")
    assert outcome.feedback == "judge exploded"
    subctx.close.assert_called_once_with()
    assert warnings.summaries == ["candidate g2c1 evaluation raised | judge exploded"]


def test_candidate_teardown_failure_does_not_hide_the_result(warnings: _Warnings) -> None:
    subctx = MagicMock()
    subctx.close.side_effect = OSError("worktree busy")
    expected = _private("_CandidateOutcome")(
        passed=True, parent_id=1, inspiration_ids=[], summary="ok", feedback="", commit="child"
    )
    parent_ctx = MagicMock()
    with (
        patch.object(subject, "create_candidate_context", return_value=subctx),
        patch.object(subject, "_evaluate_candidate", return_value=expected),
    ):
        outcome = _evaluate(parent_ctx)

    assert outcome is expected
    parent_ctx.git.retain_candidate.assert_called_once_with("g2c1", "child")
    assert warnings.summaries == ["candidate g2c1 teardown failed | worktree busy"]


def test_failed_wip_seed_snapshot_is_reported_and_seed_is_still_recorded(
    warnings: _Warnings,
) -> None:
    ctx = MagicMock()
    ctx.git.current_sha.side_effect = OSError("git broke")
    population = Population([])

    with (
        patch.object(subject, "EvolutionStateStore"),
        patch.object(subject, "_persist_evolve_state"),
    ):
        _private("_record_failed_bootstrap")(
            ctx, population, 1, SimpleNamespace(summary="tried"), "bad output\nsecond line"
        )

    assert warnings.summaries == ["wip-seed snapshot failed | git broke"]
    (failed,) = population.all
    assert (failed.passed, failed.commit, failed.summary) == (False, None, "tried")
    assert failed.feedback == "bad output\nsecond line"
    ctx.lprint.assert_called_with("[bootstrap 1] FAILED — feedback: bad output")


def test_final_report_without_a_passing_individual() -> None:
    ctx = MagicMock()

    _private("_report_final_population")(ctx, Population([]), MetricSpace())

    ctx.lprint.assert_called_once_with("\nNo passing individual produced. Inspect logs.")
    ctx.git.checkout_tree.assert_not_called()


def test_final_report_with_empty_frontier_still_materializes_latest_passer() -> None:
    ctx = MagicMock()
    ctx.git.checkout_tree.return_value = True
    space = MetricSpace(objectives=(Objective(name="tput", direction="max"),))
    population = Population([_individual(1, commit="c1"), _individual(2, commit="c2")])

    _private("_report_final_population")(ctx, population, space)

    lines = [call.args[0] for call in ctx.lprint.call_args_list]
    assert lines[0] == "\nFrontier is empty (no individual reported all objective metrics)."
    assert lines[1].startswith("\nFinal scalar-best: individual #2")
    ctx.git.checkout_tree.assert_called_once_with("c2", clean=True)


def _search_for_run(population: Population) -> _Dynamic:
    return _search(population, space=MetricSpace(), bootstrap_max_attempts=1)


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (KeyboardInterrupt(), "[evolutionary] interrupted; population preserved."),
        (RuntimeError("boom"), "[evolutionary] aborted with: boom"),
    ],
)
def test_run_evolve_search_reports_interrupts_and_unexpected_failures(
    failure: BaseException, message: str
) -> None:
    ctx = MagicMock()
    search = _search_for_run(Population([_individual(1)]))

    with patch.object(subject, "_run_evolve_generations", side_effect=failure):
        result = _private("_run_evolve_search")(ctx, search)

    assert result is False
    ctx.lprint.assert_called_with(message)


def test_run_evolve_loop_closes_context_when_policy_setup_is_interrupted() -> None:
    ctx = MagicMock()
    with (
        patch.object(subject, "_create_evolve_context", return_value=(ctx, MagicMock())),
        patch.object(subject, "_initialize_evolve_run", side_effect=KeyboardInterrupt),
    ):
        result = subject.run_evolve_loop(MagicMock())

    assert result is False
    ctx.lprint.assert_called_once_with(
        "[evolutionary] interrupted during search-policy initialization."
    )
    ctx.close.assert_called_once_with()


def test_evolve_requires_a_name_for_fresh_runs_and_an_objective() -> None:
    request = SimpleNamespace(input_bundle=MagicMock(), config=None, resume=None, exp_name=None)

    with pytest.raises(ValueError, match="exp_name must be set for a fresh"):
        _private("_create_evolve_context")(request, None)
    with pytest.raises(ValueError, match="outer loop 'evolve' must set objective"):
        _private("_required_evolve_objective")(SimpleNamespace(objective=None))
    assert _private("_required_evolve_objective")(SimpleNamespace(objective="go")) == "go"


class _StopAttemptError(Exception):
    """Ends the bootstrap attempt right after the WIP-seed checkout under test."""


def test_bootstrap_falls_back_to_reference_when_wip_seed_cannot_be_checked_out(
    warnings: _Warnings,
) -> None:
    ctx = MagicMock()
    ctx.git.checkout_tree.return_value = False
    failed_seed = _individual(1, commit="deadbeefcafe", passed=False)
    search = _search(
        Population([failed_seed]),
        bootstrap_max_attempts=1,
        keep_deployments=False,
    )

    with (
        patch.object(subject, "EvolutionStateStore"),
        patch.object(subject, "_candidate_runtime_notes", side_effect=_StopAttemptError),
        pytest.raises(_StopAttemptError),
    ):
        _private("_bootstrap_seed")(ctx, search)

    assert warnings.summaries == [
        "could not check out WIP seed 1 (commit deadbeef); starting from reference | "
    ]
    ctx.lprint.assert_any_call("\n--- bootstrap attempt 1/1 ---\n")
