"""Architecture contract: a prompt names a progress entry only when one was written.

Prompts tell agents that some text "is in the latest progress entry". That is
true only if a writer appended the text before the prompt was sent. The
mechanism is a value: ``ProgressLog.append`` is the only way to obtain a
``ProgressEntry`` and returns one after the section is on disk. A context field
typed ``ProgressEntry | None`` therefore holds an entry exactly when the text
was written.

This check parses every template under ``src/`` and requires each run of text
that names a progress entry to sit inside an ``{% if %}`` (not an ``else``)
whose test reads a context field typed ``ProgressEntry | None`` on every
Pydantic model under ``vibesys.orchestration`` that declares it. A new pointer
needs no list entry here: an unguarded pointer, or one guarded by a field of
any other type, fails.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from jinja2 import Environment, nodes
from pydantic import BaseModel

import vibesys.orchestration
from vibesys.orchestration.progress import ProgressEntry

_REPO_ROOT = Path(__file__).resolve().parents[2]
_POINTER = re.compile(r"\bprogress entr(?:y|ies)\b", re.IGNORECASE)
_ENTRY_TYPE = ProgressEntry | None


@dataclass(frozen=True, order=True)
class _Pointer:
    template: str
    line: int
    guards: tuple[str, ...]


def _guard_names(test: nodes.Node) -> set[str]:
    return {name.name for name in test.find_all(nodes.Name)} | (
        {test.name} if isinstance(test, nodes.Name) else set()
    )


def _walk(node: nodes.Node, guards: tuple[str, ...], template: str) -> list[_Pointer]:
    found: list[_Pointer] = []
    if isinstance(node, nodes.TemplateData) and _POINTER.search(" ".join(node.data.split())):
        found.append(_Pointer(template, node.lineno, guards))
    if isinstance(node, nodes.If):
        inner = guards + tuple(sorted(_guard_names(node.test)))
        for child in node.body:
            found += _walk(child, inner, template)
        for branch in node.elif_:
            found += _walk(branch, guards, template)
        for child in node.else_:
            found += _walk(child, guards, template)
        return found
    for child in node.iter_child_nodes():
        found += _walk(child, guards, template)
    return found


def _pointers(source: str, template: str) -> list[_Pointer]:
    # Parse only; autoescape has no effect on the syntax tree.
    env = Environment(autoescape=True, trim_blocks=True, lstrip_blocks=True)
    return _walk(env.parse(source), (), template)


def _field_annotations() -> dict[str, set[object]]:
    """Each context field name under ``vibesys.orchestration`` with every type it has."""
    annotations: dict[str, set[object]] = {}
    package = vibesys.orchestration
    for info in pkgutil.walk_packages(package.__path__, f"{package.__name__}."):
        module = importlib.import_module(info.name)
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, BaseModel)
                and value.__module__ == info.name
            ):
                for name, field in value.model_fields.items():
                    annotations.setdefault(name, set()).add(field.annotation)
    return annotations


def _unproven(pointers: list[_Pointer], annotations: dict[str, set[object]]) -> list[str]:
    def proves(guard: str) -> bool:
        types = annotations.get(guard, set())
        return bool(types) and types == {_ENTRY_TYPE}

    return [
        f"{p.template}:{p.line}: names a progress entry without a ProgressEntry guard "
        f"(guards: {', '.join(p.guards) or 'none'})"
        for p in sorted(pointers)
        if not any(proves(guard) for guard in p.guards)
    ]


def _repo_pointers() -> list[_Pointer]:
    return [
        pointer
        for path in sorted((_REPO_ROOT / "src").rglob("*.j2"))
        for pointer in _pointers(
            path.read_text(encoding="utf-8"), path.relative_to(_REPO_ROOT).as_posix()
        )
    ]


_ANNOTATIONS = _field_annotations()


def test_every_progress_entry_pointer_is_guarded_by_a_written_entry() -> None:
    pointers = _repo_pointers()

    assert pointers, "the scan found no progress-entry pointer; the pattern no longer matches"
    assert _unproven(pointers, _ANNOTATIONS) == []


_REPORTED = {
    "unguarded": "The latest progress entry contains the notice.",
    "guarded by data": "{% if notice %}The latest progress entry contains it.{% endif %}",
    "in the else branch": "{% if entry %}x{% else %}The latest progress entry has it.{% endif %}",
    "wrapped line": "{% if notice %}Read the latest progress\nentry first.{% endif %}",
}

_ACCEPTED = {
    "guarded by an entry": "{% if entry %}The latest progress entry contains it.{% endif %}",
    "nested guard": "{% if flag %}{% if entry %}See the progress entry.{% endif %}{% endif %}",
    "conjunction": "{% if entry and flag %}See the progress entry.{% endif %}",
    "no pointer": "Read the objective.",
}

_SNIPPET_ANNOTATIONS: dict[str, set[object]] = {
    "entry": {_ENTRY_TYPE},
    "notice": {str | None},
    "flag": {bool},
}


@pytest.mark.parametrize("source", _REPORTED.values(), ids=_REPORTED.keys())
def test_check_reports_pointers_without_a_written_entry(source: str) -> None:
    assert _unproven(_pointers(source, "t.j2"), _SNIPPET_ANNOTATIONS)


@pytest.mark.parametrize("source", _ACCEPTED.values(), ids=_ACCEPTED.keys())
def test_check_accepts_pointers_guarded_by_a_written_entry(source: str) -> None:
    assert _unproven(_pointers(source, "t.j2"), _SNIPPET_ANNOTATIONS) == []
