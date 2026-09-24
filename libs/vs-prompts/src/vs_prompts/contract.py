"""Static analysis of what a template actually references.

Two failure directions exist for a rendered template versus the context a
caller builds for it, and they need different tools:

- **Under-supply** — the template references a variable no caller ever
  passes. ``TemplateRenderer``'s ``StrictUndefined`` catches this at render
  time (see ``renderer.py``).
- **Over-supply** — a variable *is* passed, but a template silently never
  references it, so its content never reaches the rendered output. Jinja's
  ``render(**kwargs)`` accepts and discards unused kwargs by design; no
  undefined-variable check, strict or not, can see this direction. Catching
  it requires knowing what a template *should* reference and comparing that
  against what it actually does — that's what this module is for.

:func:`resolve_free_variables` parses a template and recursively follows
statically-named ``{% include %}``s (which share the parent scope by
default in Jinja) to compute the full set of free variables a render call
must supply. :class:`TemplateContract` uses that to flag templates in a
family that silently diverge from a required variable set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from jinja2 import Environment, TemplateSyntaxError, meta, nodes

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path
_env = Environment()


@dataclass(frozen=True)
class UnresolvedInclude:
    """A ``{% include %}`` whose target isn't a static string literal.

    Its variables can't be statically resolved; callers that know the finite
    set of reachable targets (e.g. one file per modality) should render
    :func:`resolve_free_variables` on each candidate themselves and union the
    results in.
    """

    template_path: Path
    source: str


def resolve_free_variables(
    template_path: Path,
    *,
    search_roots: Sequence[Path],
    _seen: frozenset[Path] | None = None,
) -> tuple[frozenset[str], tuple[UnresolvedInclude, ...]]:
    """Return the free (undeclared) variables ``template_path`` references.

    Recurses into every statically-named ``{% include %}`` (resolved against
    ``search_roots`` in order, matching ``FileSystemLoader`` multi-root
    lookup) because included templates share the including template's scope
    by default in Jinja — a variable only referenced inside an include is
    still a real requirement on the caller. ``{% include ... without context
    %}`` is treated as its own render (still recursed into, since it still
    needs *some* source for its variables — just not this scope) rather than
    silently skipped.

    Dynamic includes (the target isn't a string literal, e.g. ``{% include
    "_modality/" ~ modality ~ "/profiler.j2" %}``) can't be resolved
    statically; each is returned in the second tuple element instead of
    raising, so callers can decide how to enumerate reachable targets.
    """
    seen = _seen or frozenset()
    if template_path in seen:
        return frozenset(), ()
    seen = seen | {template_path}

    try:
        source = template_path.read_text()
        ast = _env.parse(source)
    except (OSError, TemplateSyntaxError) as exc:
        raise ValueError(f"Cannot parse template {template_path}: {exc}") from exc

    free = frozenset(meta.find_undeclared_variables(ast))
    unresolved: list[UnresolvedInclude] = []

    for node in ast.find_all(nodes.Include):
        if isinstance(node.template, nodes.Const) and isinstance(node.template.value, str):
            inc_name = node.template.value
            resolved_path = _resolve_include(inc_name, search_roots)
            if resolved_path is None:
                unresolved.append(UnresolvedInclude(template_path, inc_name))
                continue
            inc_free, inc_unresolved = resolve_free_variables(
                resolved_path, search_roots=search_roots, _seen=seen
            )
            free = free | inc_free
            unresolved.extend(inc_unresolved)
        else:
            unresolved.append(UnresolvedInclude(template_path, _describe(node.template)))

    return free, tuple(unresolved)


def _resolve_include(name: str, search_roots: Sequence[Path]) -> Path | None:
    for root in search_roots:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _describe(expr: nodes.Node) -> str:
    return f"<dynamic expression at line {expr.lineno}>"


def filter_skip_marked(
    path: Path,
    names: Iterable[str],
    *,
    skip_marker: str = "vs-prompts:unused",
) -> frozenset[str]:
    """Drop any name explicitly marked deliberate in ``path``'s source.

    A template marks a name as an intentional, reviewed omission with
    ``{# <skip_marker>: <name> - <reason> #}`` — the same convention
    :class:`~vs_prompts.fragments.FragmentFamily` uses an empty file for.
    Shared by :class:`TemplateContract` and by callers checking
    :meth:`~vs_prompts.renderer.TemplateRenderer.unused_kwargs` results, so
    both directions honor the same marker.
    """
    text = path.read_text()
    return frozenset(name for name in names if f"{skip_marker}: {name}" not in text)


@dataclass(frozen=True)
class ContractViolation:
    """A template that silently omits one or more required variables."""

    path: Path
    missing_vars: frozenset[str]

    def __str__(self) -> str:
        """Render as ``<path>: silently drops [...]`` for assertion messages and logs."""
        return f"{self.path}: silently drops {sorted(self.missing_vars)}"


@dataclass(frozen=True)
class TemplateContract:
    """A required-variable set every template in a sibling family must satisfy.

    A template that doesn't reference a required variable is a violation
    unless its source contains an explicit skip marker naming that variable —
    ``{# <skip_marker>: <var> - <reason> #}`` — so "deliberately unused" is
    distinguishable from "forgotten" the same way an empty fragment file
    marks a deliberate :class:`~vs_prompts.fragments.FragmentFamily` skip.
    """

    required: frozenset[str]
    skip_marker: str = "vs-prompts:unused"

    def check(
        self,
        paths: Iterable[Path],
        *,
        search_roots: Sequence[Path],
    ) -> list[ContractViolation]:
        """Return a :class:`ContractViolation` for each path missing a required var."""
        violations = []
        for path in paths:
            free_vars, _ = resolve_free_variables(path, search_roots=search_roots)
            missing = filter_skip_marked(
                path, self.required - free_vars, skip_marker=self.skip_marker
            )
            if missing:
                violations.append(ContractViolation(path=path, missing_vars=missing))
        return violations
