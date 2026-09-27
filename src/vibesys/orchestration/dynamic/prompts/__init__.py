"""Prompt rendering for the dynamic orchestration."""

from pathlib import Path

from vs_prompts.api import TemplateRenderer

_RENDERER = TemplateRenderer(Path(__file__).parent)


def render_portfolio(**context: object) -> str:
    """Render one compact portfolio planning request."""
    return _RENDERER.render_template("portfolio.j2", **context)


def render_implementation(**context: object) -> str:
    """Render one isolated hypothesis implementation request."""
    return _RENDERER.render_template("implement.j2", **context)


def render_review(**context: object) -> str:
    """Render one independent candidate review request."""
    return _RENDERER.render_template("review.j2", **context)


__all__ = ["render_implementation", "render_portfolio", "render_review"]
