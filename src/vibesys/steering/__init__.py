"""Policy for rendering live operator steering into an agent prompt."""

from collections.abc import Sequence
from pathlib import Path

from vs_prompts.api import TemplateRenderer

_RENDERER = TemplateRenderer(Path(__file__).parent / "prompts")


def splice_steering(user_prompt: str, messages: Sequence[str]) -> str:
    """Append queued operator steering to *user_prompt*."""
    if not messages:
        return user_prompt
    return _RENDERER.render_template(
        "operator_steering.j2", user_prompt=user_prompt, messages=list(messages)
    )


__all__ = ["splice_steering"]
