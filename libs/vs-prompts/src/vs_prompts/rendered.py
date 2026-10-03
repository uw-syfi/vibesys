"""The typed result of rendering a prompt template.

Agent-bound text is a template: Python passes data, the template owns the
wording, conditionals, and loops. :class:`RenderedPrompt` marks text that came
out of :class:`~vs_prompts.api.TemplateRenderer`, so a consumer can tell a
rendered prompt from text assembled in Python.

Only the renderer constructs one: the constructor requires a private token
held by this package, and any other caller gets ``TypeError``. During the
migration of existing call sites the type subclasses ``str`` so current
``str`` consumers keep working. Concatenation (``+``, f-strings, ``.join``)
returns a plain ``str``, so appending to rendered output loses the type.
``tests/architecture/test_prompt_templates.py`` catches the rest.
"""

from __future__ import annotations

from typing import Self

_RENDER_TOKEN = object()


class RenderedPrompt(str):
    """Prompt text produced by a template render. Immutable, renderer-only."""

    __slots__ = ()

    def __new__(cls, text: str, *, token: object) -> Self:
        """Wrap ``text``; ``token`` must be the renderer's private token."""
        if token is not _RENDER_TOKEN:
            message = "RenderedPrompt is produced only by vs_prompts.api.TemplateRenderer"
            raise TypeError(message)
        return super().__new__(cls, text)


def rendered(text: str) -> RenderedPrompt:
    """Mark renderer output as a :class:`RenderedPrompt` (renderer-internal)."""
    return RenderedPrompt(text, token=_RENDER_TOKEN)
