"""Named fragment families: one small Jinja snippet per (key, name) pair.

Generalizes the "every variant must define every required snippet, an empty
file is a deliberate skip" contract to any discriminator, not just a specific
enum. A parent template references a fragment by name (e.g. ``{{
device_dtype }}``) without knowing which key selected it; the caller
auto-injects :meth:`FragmentFamily.render_all` as kwargs on every render.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from vs_prompts.renderer import TemplateRenderer


@dataclass(frozen=True)
class FragmentFamily:
    """Fragments under ``root/<key>/<name>.j2`` for every ``name`` in ``names``.

    Parameters
    ----------
    root:
        Directory holding one subdirectory per key (e.g. ``prompts/backend``).
        Must be ``renderer.root`` or one of ``renderer.fallback_roots`` (or a
        descendant of one of those), the same multi-root lookup
        ``TemplateRenderer`` itself does, for :meth:`render`/:meth:`render_all`
        to resolve a template name against the renderer's search path.
    names:
        The canonical set of fragment stems every key must provide a file
        for. An empty file is a deliberate, reviewable skip (renders to
        ``""``); a missing file is a gap :meth:`validate` refuses to ignore.
    """

    root: Path
    names: frozenset[str]

    def validate(self, keys: Iterable[str]) -> None:
        """Raise ``ValueError`` listing every ``<key>/<name>.j2`` file missing.

        Checks every key up front rather than lazily per-render, so a gap in
        a rarely-exercised key surfaces at test/import time instead of the
        first time that key is actually selected at runtime.
        """
        missing = [
            str(self.root / str(key) / f"{name}.j2")
            for key in keys
            for name in sorted(self.names)
            if not (self.root / str(key) / f"{name}.j2").is_file()
        ]
        if missing:
            raise ValueError(
                f"FragmentFamily at {self.root}: missing fragment files: "
                f"{', '.join(missing)}. Use an empty file for a deliberate skip."
            )

    def render(self, key: str, name: str, renderer: TemplateRenderer) -> str:
        """Render a single fragment by key and name (escape hatch).

        Strips trailing newlines: fragments are inline substitutions (``{{
        stem }}`` mid-line), so the parent template owns surrounding
        whitespace.
        """
        if name not in self.names:
            raise ValueError(
                f"Unknown fragment {name!r} for {self.root}; valid: {sorted(self.names)}"
            )
        template_name = f"{self._relative_to_search_path(renderer)}/{key}/{name}.j2"
        return renderer.render_template(template_name).rstrip("\n")

    def _relative_to_search_path(self, renderer: TemplateRenderer) -> str:
        for candidate_root in (renderer.root, *renderer.fallback_roots):
            try:
                return self.root.relative_to(candidate_root).as_posix()
            except ValueError:
                continue
        raise ValueError(
            f"{self.root} is not {renderer.root} or a descendant of it or of any "
            f"fallback root {list(renderer.fallback_roots)}"
        )

    def render_all(self, key: str, renderer: TemplateRenderer) -> dict[str, str]:
        """Render every fragment in :attr:`names` for ``key``, keyed by name."""
        return {name: self.render(key, name, renderer) for name in self.names}
