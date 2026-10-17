"""Run-specific additions to an input bundle's objective."""

from pathlib import Path

from vs_prompts.api import TemplateRenderer

_RENDERER = TemplateRenderer(Path(__file__).parent / "prompts")


def with_operator_constraints(objective: str, constraints: list[str]) -> str:
    """Add run-specific invariants without mutating the input bundle."""
    normalized = [constraint.strip() for constraint in constraints if constraint.strip()]
    if not normalized:
        return objective
    return _RENDERER.render_template(
        "operator_constraints.j2", objective=objective, constraints=normalized
    )


__all__ = ["with_operator_constraints"]
