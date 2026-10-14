"""Contracts for the agent-skill convention check (`scripts/check_skills.py`).

Every case builds a throwaway repository in `tmp_path` and passes its file list
in, so no case runs Git. Each rule has a violating case paired with a compliant
one, because a check that matched nothing would also report nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from scripts.check_skills import (
    MAX_REFERENCE_LINES,
    MAX_SKILL_LINES,
    AllowlistEntry,
    AllowlistError,
    FrontmatterError,
    Rule,
    SkillKind,
    Violation,
    check_tree,
    load_allowlist,
    parse_frontmatter,
    ratchet,
    validate_frontmatter,
)

if TYPE_CHECKING:
    from pathlib import Path

TMP = [HealthCheck.function_scoped_fixture]
NAMES = st.from_regex(r"\A[a-z][a-z0-9]{1,8}-[a-z][a-z0-9]{1,8}\Z")
WORDS = (
    st.lists(st.from_regex(r"\A[a-z]{2,9}\Z"), min_size=1, max_size=12)
    .map(" ".join)
    .filter(lambda words: "vibesys" not in words)
)

CODEBASE_BODY = """# Change thing

## Builds on

- `software-design`.

## When this applies

- Changing things.

## Steps

1. Add the thing; `tests/test_thing.py` checks it.
2. Name the thing well. Unenforced: needs judgment.

## Testing

- Ban the import with `TID251`.

## Known failure modes

- Forgetting the check: `scripts/check_thing.py:main` catches it.

## Sources of truth

- The config.
"""

BASE_FILES = {
    "pyproject.toml": '[tool.ruff.lint]\nselect = ["TID", "E"]\nignore = ["E501"]\n',
    "tests/test_thing.py": "def test_thing() -> None:\n    pass\n",
    "scripts/check_thing.py": "def main() -> int:\n    return 0\n",
    "libs/vs-thing/tests/test_store.py": "def test_store() -> None:\n    pass\n",
    "src/pkg/parse_test.py": "def test_parse() -> None:\n    pass\n",
    "src/pkg/parse.py": "def parse() -> None:\n    pass\n",
    "clients/libs/run-state/src/fold.test.ts": "test('folds', () => {});\n",
    "clients/apps/web/e2e/flow.spec.ts": "test('flows', () => {});\n",
    "clients/libs/run-state/src/fold.ts": "export const fold = 1;\n",
}


def _skill_md(name: str, kind: str, body: str = "# Skill\n\nDo the thing.\n") -> str:
    """A `SKILL.md` with valid frontmatter for ``name`` and ``kind``."""
    return (
        f"---\nname: {name}\ndescription: Does a thing. Use when doing it.\n"
        f"metadata:\n  kind: {kind}\n---\n\n{body}"
    )


def _index(kinds: dict[str, str]) -> str:
    """A skills index listing each skill under its kind's heading."""
    lines = ["# Agent skills", ""]
    for kind in SkillKind:
        lines += [f"## {kind.value.capitalize()}", ""]
        lines += [f"- [{n}]({n}/SKILL.md): a skill." for n, k in kinds.items() if k == kind.value]
        lines.append("")
    return "\n".join(lines)


def _repo(
    root: Path, skills: dict[str, str], extra: dict[str, str] | None = None
) -> tuple[str, ...]:
    """Write a repository holding ``skills`` (name to kind) plus ``extra`` files."""
    files = dict(BASE_FILES)
    for name, kind in skills.items():
        files[f".agents/skills/{name}/SKILL.md"] = _skill_md(name, kind)
    files[".agents/skills/README.md"] = _index(skills)
    files.update(extra or {})
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tuple(sorted(files))


def _rules(root: Path, tracked: tuple[str, ...]) -> list[Rule]:
    return [violation.rule for violation in check_tree(root, tracked)]


def test_a_tree_of_all_three_kinds_that_follows_the_convention_passes(tmp_path: Path) -> None:
    skills = {"testing": "generic", "change-thing": "codebase", "open-pr": "workflow"}
    tracked = _repo(
        tmp_path,
        skills,
        {
            ".agents/skills/change-thing/SKILL.md": _skill_md(
                "change-thing", "codebase", CODEBASE_BODY
            )
        },
    )

    assert check_tree(tmp_path, tracked) == []


@settings(suppress_health_check=TMP)
@given(
    key=st.from_regex(r"\A[a-z][a-z_]{0,10}\Z").filter(
        lambda k: k not in {"name", "description", "metadata"}
    )
)
def test_an_unknown_frontmatter_key_is_rejected_by_name(key: str) -> None:
    fields = parse_frontmatter(
        _skill_md("open-pr", "workflow").replace("---\n\n", f"{key}: x\n---\n\n", 1)
    )

    with pytest.raises(FrontmatterError, match=f"`{key}`"):
        validate_frontmatter(fields, "open-pr")


@given(key=st.from_regex(r"\A[a-z][a-z_]{0,10}\Z").filter(lambda k: k != "kind"))
def test_an_unknown_metadata_key_is_rejected_by_name(key: str) -> None:
    fields = parse_frontmatter(
        _skill_md("open-pr", "workflow").replace("  kind:", f"  {key}: x\n  kind:")
    )

    with pytest.raises(FrontmatterError, match=f"metadata.{key}"):
        validate_frontmatter(fields, "open-pr")


@given(name=NAMES, words=WORDS, folded=st.booleans())
def test_frontmatter_reads_plain_and_folded_descriptions(
    name: str, words: str, *, folded: bool
) -> None:
    description = f"{words}. Use when {words}."
    value = f">-\n  {description}" if folded else description
    text = f"---\nname: {name}\ndescription: {value}\nmetadata:\n  kind: codebase\n---\n"

    frontmatter = validate_frontmatter(parse_frontmatter(text), name)

    assert (frontmatter.name, frontmatter.description, frontmatter.kind) == (
        name,
        description,
        SkillKind.CODEBASE,
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("name: open-pr\n", "open with a `---`"),
        ("---\nname: open-pr\n", "no closing"),
        ("---\n- item\n---\n", "expected `key: value`"),
        ("---\nname: a\nname: b\n---\n", "duplicate key"),
    ],
)
def test_malformed_frontmatter_names_the_problem(text: str, message: str) -> None:
    with pytest.raises(FrontmatterError, match=message):
        parse_frontmatter(text)


@pytest.mark.parametrize(
    ("replace", "by", "message"),
    [
        ("name: open-pr", "name: other", "directory name"),
        ("Use when doing it", "Does more", "Use when"),
        ("kind: workflow", "kind: recipe", "one of generic, codebase, workflow"),
    ],
)
def test_invalid_frontmatter_values_are_reported(replace: str, by: str, message: str) -> None:
    fields = parse_frontmatter(_skill_md("open-pr", "workflow").replace(replace, by))

    with pytest.raises(FrontmatterError, match=message):
        validate_frontmatter(fields, "open-pr")


@settings(suppress_health_check=TMP, max_examples=10)
@given(extra=st.integers(min_value=-20, max_value=20))
def test_skill_md_line_limit(tmp_path: Path, extra: int) -> None:
    lines = MAX_SKILL_LINES + extra
    head = _skill_md("open-pr", "workflow", "")
    body = "line\n" * (lines - len(head.splitlines()))
    tracked = _repo(
        tmp_path, {"open-pr": "workflow"}, {".agents/skills/open-pr/SKILL.md": head + body}
    )

    found = [v for v in check_tree(tmp_path, tracked) if v.rule is Rule.SKILL_LINES]

    assert [v.count for v in found] == ([lines] if extra > 0 else [])


@settings(suppress_health_check=TMP, max_examples=10)
@given(extra=st.integers(min_value=-5, max_value=5))
def test_reference_line_limit(tmp_path: Path, extra: int) -> None:
    lines = MAX_REFERENCE_LINES + extra
    body = "# Skill\n\nRead [the guide](references/guide.md) when guiding.\n"
    tracked = _repo(
        tmp_path,
        {"open-pr": "workflow"},
        {
            ".agents/skills/open-pr/SKILL.md": _skill_md("open-pr", "workflow", body),
            ".agents/skills/open-pr/references/guide.md": "line\n" * lines,
        },
    )

    found = [v for v in check_tree(tmp_path, tracked) if v.rule is Rule.REFERENCE_LINES]

    assert [v.count for v in found] == ([lines] if extra > 0 else [])


def test_a_reference_not_linked_from_skill_md_is_reported(tmp_path: Path) -> None:
    tracked = _repo(
        tmp_path, {"open-pr": "workflow"}, {".agents/skills/open-pr/references/guide.md": "x\n"}
    )

    assert _rules(tmp_path, tracked) == [Rule.UNLINKED_REFERENCE]


@pytest.mark.parametrize(
    "relative", ["reference.md", "tool.sh", "references/deep/guide.md", "notes/a.md"]
)
def test_files_outside_the_skill_layout_are_reported(tmp_path: Path, relative: str) -> None:
    tracked = _repo(
        tmp_path, {"open-pr": "workflow"}, {f".agents/skills/open-pr/{relative}": "x\n"}
    )

    assert Rule.LAYOUT in _rules(tmp_path, tracked)


def test_a_script_no_test_or_ci_runs_is_reported_and_a_run_one_is_not(tmp_path: Path) -> None:
    script = {".agents/skills/open-pr/scripts/tool.sh": "echo\n"}
    unrun = _repo(tmp_path / "unrun", {"open-pr": "workflow"}, script)
    run = _repo(
        tmp_path / "run",
        {"open-pr": "workflow"},
        {**script, "tests/test_tool.py": "SCRIPT = '.agents/skills/open-pr/scripts/tool.sh'\n"},
    )

    assert _rules(tmp_path / "unrun", unrun) == [Rule.UNRUN_SCRIPT]
    assert _rules(tmp_path / "run", run) == []


@pytest.mark.parametrize(
    ("span", "resolves"),
    [
        ("scripts/check_thing.py", True),
        ("scripts/gone.py", False),
        ("scripts/check_thing.py:main", True),
        ("scripts/check_thing.py:gone", False),
        ("tests/", True),
        ("scripts/*.py", True),
        ("scripts/*.ts", False),
        ("references/guide.md", True),
    ],
)
def test_references_in_a_skill_must_resolve(tmp_path: Path, span: str, *, resolves: bool) -> None:
    body = f"# Skill\n\nSee `{span}` and [the guide](references/guide.md).\n"
    tracked = _repo(
        tmp_path,
        {"open-pr": "workflow"},
        {
            ".agents/skills/open-pr/SKILL.md": _skill_md("open-pr", "workflow", body),
            ".agents/skills/open-pr/references/guide.md": "x\n",
        },
    )

    assert (Rule.UNRESOLVED_REFERENCE not in _rules(tmp_path, tracked)) == resolves


@settings(suppress_health_check=TMP, max_examples=10)
@given(words=WORDS)
def test_a_generic_skill_in_plain_words_passes(tmp_path: Path, words: str) -> None:
    body = f"# Testing\n\n{words}.\n"
    tracked = _repo(
        tmp_path,
        {"testing": "generic"},
        {".agents/skills/testing/SKILL.md": _skill_md("testing", "generic", body)},
    )

    assert _rules(tmp_path, tracked) == []


@pytest.mark.parametrize(
    "name",
    [
        "`scripts/check_thing.py`",
        "tests/ directory",
        "VibeSys",
        "`vs_core.api`",
        "vs-sim",
        "AGENTS.md",
    ],
)
def test_a_generic_skill_naming_the_repository_is_reported(tmp_path: Path, name: str) -> None:
    body = f"# Testing\n\nSee {name} here.\n"
    tracked = _repo(
        tmp_path,
        {"testing": "generic"},
        {".agents/skills/testing/SKILL.md": _skill_md("testing", "generic", body)},
    )

    assert Rule.REPO_SPECIFIC_NAME in _rules(tmp_path, tracked)


def test_a_generic_skill_citing_a_codebase_skill_is_reported(tmp_path: Path) -> None:
    body = "# Testing\n\nLoad the `change-thing` skill.\n"
    tracked = _repo(
        tmp_path,
        {"testing": "generic", "change-thing": "codebase"},
        {
            ".agents/skills/testing/SKILL.md": _skill_md("testing", "generic", body),
            ".agents/skills/change-thing/SKILL.md": _skill_md(
                "change-thing", "codebase", CODEBASE_BODY
            ),
        },
    )

    assert _rules(tmp_path, tracked) == [Rule.CITES_CODEBASE_SKILL]


def test_codebase_sections_out_of_order_are_reported(tmp_path: Path) -> None:
    body = CODEBASE_BODY.replace("## Testing", "## Checks")
    tracked = _repo(
        tmp_path,
        {"change-thing": "codebase"},
        {".agents/skills/change-thing/SKILL.md": _skill_md("change-thing", "codebase", body)},
    )

    assert _rules(tmp_path, tracked) == [Rule.CODEBASE_SECTIONS]


@pytest.mark.parametrize(
    ("item", "enforced"),
    [
        ("Do it; `tests/test_thing.py` checks it.", True),
        ("Do it; `scripts/check_thing.py:main` checks it.", True),
        ("Do it; `libs/vs-thing/tests/test_store.py` checks it.", True),
        ("Do it; `src/pkg/parse_test.py:test_parse` checks it.", True),
        ("Do it; `clients/libs/run-state/src/fold.test.ts` checks it.", True),
        ("Do it; `clients/apps/web/e2e/flow.spec.ts` checks it.", True),
        ("Do it; `clients/libs/run-state/src/fold.ts` holds it.", False),
        ("Do it; `src/pkg/parse.py:parse` holds it.", False),
        ("Do it; `clients/libs/run-state/src/gone.test.ts` checks it.", False),
        ("Do it. Unenforced: needs judgment.", True),
        ("Do it; `TID251` bans the rest.", True),
        ("Do it; `E501` would catch it.", False),
        ("Do it; `ARG001` would catch it.", False),
        ("Do it; `tests/test_gone.py` checks it.", False),
        ("Do it; `pyproject.toml` says so.", False),
        ("Just do it.", False),
    ],
)
def test_a_codebase_rule_item_needs_a_resolving_check_or_the_unenforced_mark(
    tmp_path: Path, item: str, *, enforced: bool
) -> None:
    body = CODEBASE_BODY.replace("- Ban the import with `TID251`.", f"- {item}")
    tracked = _repo(
        tmp_path,
        {"change-thing": "codebase"},
        {".agents/skills/change-thing/SKILL.md": _skill_md("change-thing", "codebase", body)},
    )

    found = [v for v in check_tree(tmp_path, tracked) if v.rule is Rule.RULE_WITHOUT_ENFORCEMENT]

    assert (found == []) == enforced


def test_the_index_must_list_each_skill_once_under_its_kind_and_nothing_else(
    tmp_path: Path,
) -> None:
    wrong = _index({"open-pr": "generic", "gone": "workflow"})
    tracked = _repo(tmp_path, {"open-pr": "workflow"}, {".agents/skills/README.md": wrong})

    messages = [v.message for v in check_tree(tmp_path, tracked) if v.rule is Rule.INDEX]

    assert any("`open-pr` exactly once under `## Workflow`" in m for m in messages)
    assert any("`gone`" in m for m in messages)


KEYS = st.tuples(st.sampled_from(["a.md", "b.md"]), st.sampled_from(list(Rule)))


@given(
    recorded=st.dictionaries(KEYS, st.integers(1, 5)),
    current=st.dictionaries(KEYS, st.integers(1, 5)),
)
def test_the_ratchet_passes_only_when_counts_match_the_record(
    recorded: dict[tuple[str, Rule], int], current: dict[tuple[str, Rule], int]
) -> None:
    violations = [Violation(path, rule, 1, "m", count) for (path, rule), count in current.items()]
    allowlist = [
        AllowlistEntry(path, rule, count, "why") for (path, rule), count in recorded.items()
    ]

    errors = ratchet(violations, allowlist)

    assert (errors == []) == (recorded == current)
    for (path, rule), count in recorded.items():
        now = current.get((path, rule), 0)
        if now > count:
            assert any(f"grew from {count} to {now}" in e for e in errors)
        if now < count:
            assert any(f"shrank from {count} to {now}" in e for e in errors)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[{"path": "a", "rule": "layout", "count": 1}]', "keys must be"),
        ('[{"path": "a", "rule": "nope", "count": 1, "reason": "r"}]', "unknown rule `nope`"),
        ('[{"path": "a", "rule": "layout", "count": 0, "reason": "r"}]', "positive integer"),
    ],
)
def test_a_malformed_allowlist_is_rejected_with_the_entry(text: str, message: str) -> None:
    with pytest.raises(AllowlistError, match=message):
        load_allowlist(text)


def test_a_well_formed_allowlist_loads() -> None:
    text = '[{"path": "a", "rule": "layout", "count": 2, "reason": "r"}]'

    assert load_allowlist(text) == [AllowlistEntry("a", Rule.LAYOUT, 2, "r")]
