"""Accepting a designer's reply as a plan the ledger can start, and the base it starts from."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.hypothesis import (
    InvalidPlanError,
    OrchestratorPlan,
    normalize_hypothesis_title,
    rounds,
)
from vs_core.api.search import RevisionRef, Value

if TYPE_CHECKING:
    from vibesys.hypothesis import HypothesisConfig
    from vibesys.serial_search._run import RoundStart


class Base(Value):
    """The code an idea starts from, and the round whose idea made it (None: the input)."""

    revision: RevisionRef
    round: int | None = None


def accept_plan(
    config: HypothesisConfig, ledger: rounds.Ledger, reply: OrchestratorPlan, *, start: RoundStart
) -> OrchestratorPlan | str:
    """The designer's reply as a plan the ledger can start, else what is wrong with it.

    The reply already parsed against its model. The id and title are normalized; a plan that
    updates a hypothesis twice or itself, reuses an existing id, names a base that is not the
    input or a passed idea, or updates what the ledger cannot accept is refused with the reason
    the correction turn shows the designer. Skill recommendations are not resolved on the core
    path, so the plan never carries any.
    """
    try:
        plan = reply.model_copy(
            update={
                "hypothesis_id": reply.hypothesis_id.strip() or f"hypothesis-{start.number:04d}",
                "title": normalize_hypothesis_title(reply.title),
                "recommended_skills": [],
            }
        )
        _check(plan, config, ledger, start)
    except ValueError as problem:
        return str(problem)
    return plan


def _check(
    plan: OrchestratorPlan, config: HypothesisConfig, ledger: rounds.Ledger, start: RoundStart
) -> None:
    """Raise `ValueError` for a plan the ledger cannot accept."""
    updates = [item.hypothesis_id for item in plan.hypothesis_updates]
    if len(updates) != len(set(updates)):
        raise InvalidPlanError.duplicate_updates()
    if plan.hypothesis_id in updates:
        raise InvalidPlanError.self_reference()
    if ledger.search.by_id(plan.hypothesis_id) is not None:
        raise InvalidPlanError.reused_id(plan.hypothesis_id)
    _check_base(plan, ledger, start)
    _check_bar(plan, ledger)
    rounds.validate_plan(config, ledger, plan.hypothesis_updates)


def _check_bar(plan: OrchestratorPlan, ledger: rounds.Ledger) -> None:
    """Raise `InvalidPlanError` for an expected-effect bar that asks for a loss of the metric.

    The bar is a ratio over the base in the headline metric's own direction, so it is above one
    for a ``max`` metric and below one for a ``min`` metric. A run that declares no metric has
    no direction to hold it to.
    """
    primary = ledger.search.metrics.primary
    if primary is None:
        return
    ratio = plan.minimum_ratio
    if (primary.direction == "max" and ratio < 1) or (primary.direction == "min" and ratio > 1):
        raise InvalidPlanError.bar_on_wrong_side(ratio, primary.direction)


def _check_base(plan: OrchestratorPlan, ledger: rounds.Ledger, start: RoundStart) -> None:
    """Raise `InvalidPlanError` naming a base that is neither the input nor a passed idea."""
    named = plan.base_round
    if named is None or named == 0:
        return
    passed = tuple(item.round_number for item in start.passed_ideas())
    if named in passed:
        return
    played = any(record.round_number == named for record in ledger.records)
    raise (
        InvalidPlanError.failed_base(named, passed)
        if played
        else InvalidPlanError.unknown_base(named, passed)
    )


def base_commit(plan: OrchestratorPlan, start: RoundStart, *, input_revision: RevisionRef) -> Base:
    """The base an accepted plan starts from.

    The plan's own choice when it names one (0 is the input, else a passed idea); otherwise the
    best idea so far, which the round was told (`RoundStart.base`).
    """
    named = plan.base_round
    if named is None:
        return Base(revision=start.base, round=start.best_round)
    if named == 0:
        return Base(revision=input_revision)
    chosen = next(item for item in start.passed_ideas() if item.round_number == named)
    return Base(revision=chosen.revision, round=chosen.round_number)


def continued_base(
    start: RoundStart, parent_commit: str | None, *, input_revision: RevisionRef
) -> RevisionRef:
    """The base of a round that continues its own hypothesis: its own work in progress.

    When the previous round retained nothing, the continuation starts again from the base the
    hypothesis began with.
    """
    return start.work_in_progress or revision_of(parent_commit, input_revision)


def revision_of(commit: str | None, fallback: RevisionRef) -> RevisionRef:
    """The revision a recorded commit names, else ``fallback``."""
    return fallback if not commit else RevisionRef.of_git_commit(commit)
