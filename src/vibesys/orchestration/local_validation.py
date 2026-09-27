"""Product interpretation and event projection for runtime local validation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.evaluators.gates import GATE_LOG_TAIL_CHARS, emit_gate_finished, emit_gate_started
from vibesys.events import GateFinishedData, GateKind
from vs_runtime.api import LocalValidationEvaluation
from vs_runtime.api.infrastructure import (
    FrameworkValidationResult,
    LocalValidationRecipeError,
    LocalValidationRecipeErrorKind,
    ValidationRecipe,
    run_local_validation,
)

if TYPE_CHECKING:
    from vibesys.run.event_journal import EventJournal
    from vs_runtime.api import Commands, Workspace


class _GateEvents:
    """Translate policy-neutral recipe observations to VibeSys gate events."""

    def __init__(self, events: EventJournal) -> None:
        self._events = events

    def started(self, recipe: ValidationRecipe) -> None:
        emit_gate_started(
            self._events,
            GateKind.VALIDATION,
            recipe=recipe.name,
            command=recipe.command,
        )

    def finished(self, result: FrameworkValidationResult) -> None:
        failure = None if result.passed else (result.error or result.output or "unknown failure")
        emit_gate_finished(
            self._events,
            GateFinishedData(
                gate=GateKind.VALIDATION,
                recipe=result.recipe.name,
                reused=result.reused,
                output_tail=None if failure is None else failure[-GATE_LOG_TAIL_CHARS:],
            ),
            passed=result.passed,
        )


async def validate_local(
    commands: Commands,
    events: EventJournal,
    workspace: Workspace,
    *,
    recipe_artifact: str,
    report_location: str,
) -> LocalValidationEvaluation:
    """Run the mechanism and map its detailed outcome to product feedback."""
    try:
        results = await run_local_validation(
            commands,
            workspace,
            recipe_artifact=recipe_artifact,
            report_location=report_location,
            events=_GateEvents(events),
        )
    except LocalValidationRecipeError as error:
        if error.kind is LocalValidationRecipeErrorKind.DUPLICATE_NAMES:
            feedback = "Framework local validation recipes contain duplicate names."
        else:
            feedback = f"Framework local validation recipe error: {error}."
        return LocalValidationEvaluation(passed=False, feedback=feedback)

    failed = next((result for result in results if not result.passed), None)
    if failed is None:
        return LocalValidationEvaluation(passed=True, report_location=report_location)
    detail = failed.error or failed.output or "unknown failure"
    return LocalValidationEvaluation(
        passed=False,
        feedback=(
            f"Framework local validation failed for {failed.recipe.name!r}: {detail}. "
            f"Inspect `{report_location}` and repair only the affected local contract."
        ),
        report_location=report_location,
    )


__all__ = ["validate_local"]
