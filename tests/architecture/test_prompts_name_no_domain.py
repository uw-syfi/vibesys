"""Prompt templates outside a domain pack never branch on which domain a run has.

A run's domain (``DomainName``) reaches its agents through one path: the pack under
``src/vibesys/domains/<module>/prompts/`` is rendered into ``domain_sections`` and every
strategy prompt prints it in one slot (``core_protocol/domain_section.j2``). A strategy or
shared template that tests the domain itself (``{% if facts.domain_id == "llm-serving" %}``)
puts domain text in a layer every domain shares, where other strategies never see it and the
pack cannot own it.

This check reports, in every ``.j2`` file under ``src`` and ``libs`` outside
``src/vibesys/domains``, each Jinja tag (``{% %}`` or ``{{ }}``; comments are skipped) that
reads ``domain_id`` or ``domain_name`` or quotes a registered domain name.

Existing violations are listed in ``_NOT_YET_MOVED`` with the exact number of such tags in that
file. The count is exact: a new tag fails, and so does a removed one until the entry is lowered
or deleted, so the list only shrinks. Move the text into the pack's role file instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from vibesys.constants import DomainName

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_ROOTS = (_REPO_ROOT / "src", _REPO_ROOT / "libs")
_DOMAIN_PACKS = _REPO_ROOT / "src" / "vibesys" / "domains"

_ISSUE_QUEUE = "src/vibesys/orchestration/issue_queue/core_policy/prompts"
_NOT_YET_MOVED: dict[str, int] = {
    f"{_ISSUE_QUEUE}/implement.j2": 1,
    f"{_ISSUE_QUEUE}/performance.j2": 4,
    f"{_ISSUE_QUEUE}/review.j2": 1,
}

_TAG = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)
_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_IDENTITY = re.compile(r"\bdomain_(?:id|name)\b")


def domain_branches(source: str, domains: frozenset[str]) -> list[int]:
    """The line of every Jinja tag in template ``source`` that tests the run's domain."""
    text = _COMMENT.sub(lambda comment: "\n" * comment.group().count("\n"), source)
    quoted = {f"{quote}{name}{quote}" for name in domains for quote in "\"'"}
    return [
        text.count("\n", 0, tag.start()) + 1
        for tag in _TAG.finditer(text)
        if _IDENTITY.search(tag.group()) or any(name in tag.group() for name in quoted)
    ]


_DOMAINS = frozenset(domain.value for domain in DomainName)


def _templates() -> list[Path]:
    return sorted(
        path
        for root in _TEMPLATE_ROOTS
        for path in root.rglob("*.j2")
        if not path.is_relative_to(_DOMAIN_PACKS)
    )


def _relative(template: Path) -> str:
    return template.relative_to(_REPO_ROOT).as_posix()


def _count(path: str) -> int:
    return len(domain_branches((_REPO_ROOT / path).read_text("utf-8"), _DOMAINS))


@pytest.mark.parametrize(
    "source",
    [
        '{% if facts.domain_id == "llm-serving" %}x{% endif %}',
        "{% if domain_name in ('database', 'generic') %}x{% endif %}",
        "{{ 'kernel-writing' if fast else '' }}",
        '{% if run.domain_id\n   == "microservices" %}x{% endif %}',
    ],
)
def test_the_scanner_flags_a_tag_that_tests_the_domain(source: str) -> None:
    assert domain_branches(source, _DOMAINS)


@pytest.mark.parametrize(
    "source",
    [
        "{{ domain_section(domain_sections, 'implementer') }}",
        "Prose may say llm-serving or database outside a tag.",
        '{# {% if facts.domain_id == "llm-serving" %} only a comment #}',
        "{{ domain_identifier }} {{ my_domain_id_list }}",
        '{% if kind == "llm-serving-ish" %}x{% endif %}',
    ],
)
def test_the_scanner_passes_a_tag_that_does_not(source: str) -> None:
    assert not domain_branches(source, _DOMAINS)


def test_the_scanner_reports_the_line_of_the_tag() -> None:
    source = 'a\n{# x\n#}\n{% if domain_id == "generic" %}'
    assert domain_branches(source, _DOMAINS) == [4]


def test_the_check_sees_the_strategy_templates() -> None:
    names = {_relative(path) for path in _templates()}
    assert set(_NOT_YET_MOVED) <= names
    assert not any(name.startswith("src/vibesys/domains/") for name in names)


@pytest.mark.parametrize("template", _templates(), ids=_relative)
def test_a_template_outside_a_domain_pack_does_not_test_the_domain(template: Path) -> None:
    path = _relative(template)
    lines = domain_branches(template.read_text("utf-8"), _DOMAINS)
    allowed = _NOT_YET_MOVED.get(path, 0)

    assert len(lines) == allowed, (
        f"{path}: Jinja tags on lines {lines} test the run's domain ({allowed} allowed). "
        "Put domain text in the pack's role file under src/vibesys/domains/<module>/prompts/; "
        "it reaches every strategy through domain_sections."
    )


def test_every_exemption_still_matches_exactly() -> None:
    stale = {
        path: (count, _count(path))
        for path, count in _NOT_YET_MOVED.items()
        if _count(path) != count
    }

    assert not stale, f"exemptions whose (allowed, actual) counts differ: {stale}"
