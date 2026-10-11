"""Read-only experiment-chat prompts, rendered from the templates beside this module."""

from __future__ import annotations

from pathlib import Path

from vs_prompts.api import RenderedPrompt, TemplateRenderer

_RENDERER = TemplateRenderer(Path(__file__).parent)


def experiment_chat_system_prompt() -> RenderedPrompt:
    """Build the initial read-only investigation prompt for experiment chat."""
    return _RENDERER.render_template("experiment_chat_system_prompt.j2")


def experiment_chat_continuation_prompt() -> RenderedPrompt:
    """Build the prompt used after an experiment chat has transcript history."""
    return _RENDERER.render_template("experiment_chat_continuation_prompt.j2")


__all__ = ["experiment_chat_continuation_prompt", "experiment_chat_system_prompt"]
