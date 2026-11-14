"""Jinja2 prompt rendering.

A **template** is a full prompt the LLM sees as one document (e.g. a
strategy's ``implementer.j2``). Templates live in a per-strategy directory
and fall back to the shared templates, the ``vibesys/prompts/`` root and the
core-protocol templates, in that order.

Rendering itself (Jinja environment construction and strict-undefined
enforcement) is owned by ``vs_prompts`` (``libs/vs-prompts``); this module
binds that generic machinery to ``PROMPTS_DIR``.
"""

from pathlib import Path

from vs_prompts.api import RenderedPrompt, TemplateRenderer

PROMPTS_DIR = Path(__file__).resolve().parent
CORE_PROTOCOL_DIR = PROMPTS_DIR / "core_protocol"
"""Directory of the shared core-protocol templates, a fallback root of a core policy."""
_SHARED_LOOP_FRAGMENTS_ROOT = PROMPTS_DIR / "shared"

_renderer = TemplateRenderer(PROMPTS_DIR)

# Cache of TemplateRenderers keyed by template directory path
_env_cache: dict[str, TemplateRenderer] = {str(PROMPTS_DIR): _renderer}


def _build_env(template_dir: Path | str | None = None) -> TemplateRenderer:
    """Return a ``TemplateRenderer`` for the given template directory.

    Per-loop prompt directories fall back, in order, to
    ``vibesys/prompts/shared/`` (templates shared across strategies), the
    ``vibesys/prompts/`` root itself and the core-protocol templates.
    A strategy's own folder is always searched first; strategies never
    resolve templates from a sibling strategy's folder.
    """
    if template_dir is None:
        return _renderer
    key = str(template_dir)
    if key not in _env_cache:
        _env_cache[key] = (
            _renderer
            if key == str(PROMPTS_DIR)
            else TemplateRenderer(
                Path(template_dir),
                fallback_roots=(
                    _SHARED_LOOP_FRAGMENTS_ROOT,
                    PROMPTS_DIR,
                    CORE_PROTOCOL_DIR,
                ),
            )
        )
    return _env_cache[key]


def render_template(
    name: str,
    *,
    template_dir: Path | str | None = None,
    **kwargs: object,
) -> RenderedPrompt:
    """Render a Jinja2 template from ``template_dir`` and its fallback roots."""
    renderer = _build_env(template_dir)
    return renderer.render_template(name, **kwargs)


def render_string(source: str, **kwargs: object) -> RenderedPrompt:
    """Render a Jinja2 template from an in-memory string.

    Used by call sites that hold the template text directly rather than a
    Jinja template file. Shares the root environment's settings so ``{% if %}``
    trimming matches file-based templates.
    """
    return _renderer.render_string(source, **kwargs)
