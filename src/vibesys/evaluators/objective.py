"""Shared objective-text transformations."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibesys.evaluators.input_manifest import InputBundle


def load_objective(bundle: InputBundle) -> str:
    """Return one input bundle's objective text."""
    return bundle.objective


def with_operator_constraints(objective: str, constraints: list[str]) -> str:
    """Add run-specific invariants without mutating the input bundle."""
    normalized = [constraint.strip() for constraint in constraints if constraint.strip()]
    if not normalized:
        return objective
    lines = "\n".join(f"- {constraint}" for constraint in normalized)
    return f"{objective.rstrip()}\n\n## Operator constraints\n\n{lines}\n"
