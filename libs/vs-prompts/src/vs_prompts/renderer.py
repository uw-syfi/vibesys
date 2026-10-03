"""Jinja2 template rendering with ``StrictUndefined`` as the only mode.

A :class:`TemplateRenderer` is bound to a filesystem root (plus optional
fallback roots, searched in order after the primary root) and renders
templates under it with ``jinja2.StrictUndefined``: referencing a variable no
caller supplied raises ``jinja2.UndefinedError`` at render time instead of
silently producing an empty string.

``{% if x is defined %}`` / ``{{ x is defined }}`` guards keep working —
Jinja's ``defined`` test never evaluates the underlying value, so it never
triggers strict-undefined's error regardless of which ``Undefined`` class is
configured.

This module has no framework dependencies: callers own template content,
directory layout, and what context they pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Self

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from vs_prompts.contract import resolve_free_variables

if TYPE_CHECKING:
    from collections.abc import Sequence


# Private: only this module mints a RenderedPrompt. The architecture check
# tests/architecture/test_prompt_templates.py rejects any other use of it.
_RENDER_TOKEN = object()


class RenderedPrompt(str):
    """Prompt text produced by a :class:`TemplateRenderer` render. Renderer-only.

    Agent-bound text is a template: Python passes data, the template owns the
    wording, conditionals, and loops. This type marks text that came out of the
    renderer, so a consumer can tell a rendered prompt from text assembled in
    Python. The constructor requires a token private to this module; any other
    caller gets ``TypeError``.

    Transitional: it subclasses ``str`` so existing ``str`` consumers keep
    working while call sites migrate. Concatenation (``+``, f-strings,
    ``.join``) returns a plain ``str``. The architecture check catches the
    remaining escape hatches.
    """

    __slots__ = ()

    def __new__(cls, text: str, *, token: object) -> Self:
        """Wrap ``text``; ``token`` must be this module's private token."""
        if token is not _RENDER_TOKEN:
            message = "RenderedPrompt is produced only by vs_prompts.api.TemplateRenderer"
            raise TypeError(message)
        return super().__new__(cls, text)

    def __reduce__(self) -> tuple[object, tuple[str]]:
        """Copy (and pickle) as the same rendered text; the token stays private."""
        return _restore_rendered_prompt, (str(self),)


def _restore_rendered_prompt(text: str) -> RenderedPrompt:
    """Rebuild a copied or unpickled prompt; it was rendered when first minted."""
    return RenderedPrompt(text, token=_RENDER_TOKEN)


class TemplateRenderer:
    """Renders ``.j2``/text templates under ``root``, with optional fallback roots.

    A template name not found directly under ``root`` is looked up in each of
    ``fallback_roots`` in order — the same shape as a per-directory template
    root that still needs to resolve shared includes from a common root.
    """

    def __init__(self, root: Path, *, fallback_roots: Sequence[Path] = ()) -> None:
        """Bind a renderer to ``root``, searched before ``fallback_roots`` in order."""
        self.root = root
        self.fallback_roots = tuple(fallback_roots)
        self._loader = FileSystemLoader([str(root), *(str(p) for p in self.fallback_roots)])
        self._env = Environment(  # noqa: S701  # lint-waiver: LW-010110 [S701]; this renderer emits plain-text prompts, so HTML autoescaping would corrupt template content.
            loader=self._loader,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=StrictUndefined,
        )

    def render_template(self, name: str, /, **kwargs: object) -> RenderedPrompt:
        """Render the named template file. Raises ``UndefinedError`` on a missing var.

        ``name`` is positional-only so a template that legitimately wants a
        context variable called ``name`` (or ``source``, for
        :meth:`render_string`) doesn't collide with this method's own
        parameter.
        """
        return RenderedPrompt(self._env.get_template(name).render(**kwargs), token=_RENDER_TOKEN)

    def unused_kwargs(self, name: str, /, **kwargs: object) -> frozenset[str]:
        """Return which of ``kwargs`` this template would silently never use.

        The over-supply counterpart to ``StrictUndefined``: a variable a
        caller passes but a template never references costs nothing and
        raises nothing, so its content just never reaches the rendered
        output. Unlike a family-wide contract, this needs no declared or
        derived "required" set — it only compares this one call's own kwargs
        against what this one template actually reads (via the same static,
        include-recursing analysis as
        :func:`vs_prompts.contract.resolve_free_variables`, so it also
        catches a key used only inside a branch this call doesn't take).
        """
        _, filename, _ = self._loader.get_source(self._env, name)
        free_vars, _ = resolve_free_variables(
            Path(filename), search_roots=(self.root, *self.fallback_roots)
        )
        return frozenset(kwargs) - free_vars

    def render_string(self, source: str, /, **kwargs: object) -> RenderedPrompt:
        """Render a Jinja2 template held as a string rather than a file.

        Shares this renderer's environment settings (trimming, strict
        undefined) so behavior matches file-based templates rendered from the
        same root.
        """
        return RenderedPrompt(self._env.from_string(source).render(**kwargs), token=_RENDER_TOKEN)

    def child(self, subroot: Path) -> TemplateRenderer:
        """A renderer scoped to ``subroot``, falling back to this renderer's root.

        Mirrors looking up a per-mode template directory that still needs to
        resolve shared includes (fragments, partials) from the parent root.
        """
        return TemplateRenderer(subroot, fallback_roots=(self.root, *self.fallback_roots))
