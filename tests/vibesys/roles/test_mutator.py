"""Regression test for MutatorContext validation.

Verifies that MutatorContext rejects invalid combinations of is_cold_start
and parent, which would otherwise cause jinja2 UndefinedError when rendering
the template.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vibesys.roles.mutator import MutatorContext
from vibesys.search.population.models import Individual


def test_mutator_context_rejects_non_cold_start_without_parent() -> None:
    """Regression: constructing MutatorContext(is_cold_start=False, parent=None)
    must raise ValidationError, not silently succeed.

    The template mutator_prompt.j2 reads {{ parent.id }} when is_cold_start
    is False, so the invalid combination causes jinja2.UndefinedError at
    render time. The model_validator should fail fast at construction time.
    """
    with pytest.raises(ValidationError) as exc_info:
        MutatorContext(
            accuracy_command=None,
            benchmark_command=None,
            domain_implementer="",
            failed_lessons=[],
            inspirations=[],
            interface="",
            is_cold_start=False,
            modality=None,
            num_failed_attempts=0,
            objective="",
            objectives=None,
            parent=None,
            reference_path="",
            repair_seed=False,
            runtime_notes="",
        )
    error = exc_info.value
    assert len(error.errors()) == 1
    validation_error = error.errors()[0]
    assert validation_error["type"] == "value_error"
    assert "parent is required unless is_cold_start is True" in str(
        validation_error["ctx"]["error"]
    )


def test_mutator_context_allows_cold_start_without_parent() -> None:
    """is_cold_start=True allows parent=None."""
    context = MutatorContext(
        accuracy_command=None,
        benchmark_command=None,
        domain_implementer="",
        failed_lessons=[],
        inspirations=[],
        interface="",
        is_cold_start=True,
        modality=None,
        num_failed_attempts=0,
        objective="",
        objectives=None,
        parent=None,
        reference_path="",
        repair_seed=False,
        runtime_notes="",
    )
    assert context.is_cold_start
    assert context.parent is None


def test_mutator_context_allows_non_cold_start_with_parent() -> None:
    """is_cold_start=False requires parent to be present."""
    parent = Individual(
        id=1,
        generation=0,
        perf_metric=1.0,
        perf_unit="tok/s",
        summary="parent summary",
        feedback="",
        metrics={},
    )
    context = MutatorContext(
        accuracy_command=None,
        benchmark_command=None,
        domain_implementer="",
        failed_lessons=[],
        inspirations=[],
        interface="",
        is_cold_start=False,
        modality=None,
        num_failed_attempts=0,
        objective="",
        objectives=None,
        parent=parent,
        reference_path="",
        repair_seed=False,
        runtime_notes="",
    )
    assert not context.is_cold_start
    assert context.parent is not None
