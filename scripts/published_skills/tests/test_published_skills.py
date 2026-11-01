"""Contracts for the plugin manifest generator and the published-skill checks.

Every case builds a throwaway repository in ``tmp_path``. Each rule has a violating case paired
with a compliant one, because a check that matched nothing would also report nothing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from scripts.published_skills import (
    MANIFEST_PATH,
    SPEC_FIELDS,
    VIBESYS_TOOL_NAMES,
    ManifestError,
    check_manifest,
    check_skills,
    discover,
    render_manifest,
)
from scripts.published_skills.__main__ import REPOSITORY

from vs_evaluation.api.queue_status import READ_QUEUE_STATUS_TOOL
from vs_evaluation.api.tools import SUBMIT_TOOL, VALIDATE_WAIT_TOOL

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

TMP = [HealthCheck.function_scoped_fixture]
NAMES = st.from_regex(r"\A[a-z][a-z0-9]{0,8}(-[a-z0-9]{1,8}){0,2}\Z")
MANIFEST = {
    "name": "vibesys",
    "owner": {"name": "uw-syfi"},
    "plugins": [
        {"name": "vibesys", "source": "./skills", "version": "0.1.0", "strict": False, "skills": []}
    ],
}


def _skill(
    root: Path,
    relative: str,
    *,
    name: str | None = None,
    description: str = "Use when x.",
    body: str = "body\n",
) -> None:
    folder = root / "skills" / relative
    folder.mkdir(parents=True, exist_ok=True)
    shown = folder.name if name is None else name
    (folder / "SKILL.md").write_text(f"---\nname: {shown}\ndescription: {description}\n---\n{body}")


def _skill_with_fields(root: Path, relative: str, fields: str) -> None:
    """A skill whose frontmatter holds ``fields`` after a valid name and description."""
    folder = root / "skills" / relative
    folder.mkdir(parents=True, exist_ok=True)
    head = f"name: {folder.name}\ndescription: Use when x.\n{fields}"
    (folder / "SKILL.md").write_text(f"---\n{head}---\nbody\n")


def _manifest(root: Path, manifest: object = MANIFEST) -> None:
    (root / MANIFEST_PATH).parent.mkdir(parents=True, exist_ok=True)
    (root / MANIFEST_PATH).write_text(json.dumps(manifest, indent=2) + "\n")


def _messages(root: Path) -> list[str]:
    return [str(finding) for finding in check_skills(root)]


def test_discovery_lists_nested_skills_and_stops_inside_a_skill(tmp_path: Path) -> None:
    _skill(tmp_path, "search/optimize")
    _skill(tmp_path, "domains/serving-systems")
    _skill(tmp_path, "domains/pack/skills/pack-runtime")
    _skill(tmp_path, "domains/serving-systems/references/inner")
    (tmp_path / "skills/domains/.hidden/hid").mkdir(parents=True)
    (tmp_path / "skills/domains/.hidden/hid/SKILL.md").write_text("x")
    _skill(tmp_path, "other/not-published")
    _skill(tmp_path, "hooks/not-a-skill")

    assert [skill.path for skill in discover(tmp_path)] == [
        "./domains/pack/skills/pack-runtime",
        "./domains/serving-systems",
        "./search/optimize",
    ]


def test_discovery_without_a_skills_tree_is_empty(tmp_path: Path) -> None:
    assert discover(tmp_path) == []


@settings(suppress_health_check=TMP)
@given(search=st.sets(NAMES, max_size=4), domains=st.sets(NAMES, max_size=4))
def test_discovery_finds_exactly_the_skills_present_in_sorted_order(
    tmp_path_factory: pytest.TempPathFactory, search: set[str], domains: set[str]
) -> None:
    root = tmp_path_factory.mktemp("tree")
    for name in search:
        _skill(root, f"search/{name}")
    for name in domains:
        _skill(root, f"domains/{name}")

    paths = [skill.path for skill in discover(root)]

    assert paths == sorted(paths)
    assert set(paths) == {f"./search/{n}" for n in search} | {f"./domains/{n}" for n in domains}


def test_render_replaces_only_the_skill_list(tmp_path: Path) -> None:
    _skill(tmp_path, "domains/kernel-performance")
    _manifest(tmp_path, {**MANIFEST, "description": "kept"})

    rendered = json.loads(render_manifest(tmp_path))

    assert rendered["description"] == "kept"
    assert rendered["plugins"][0]["skills"] == ["./domains/kernel-performance"]
    assert rendered["plugins"][0]["version"] == "0.1.0"
    assert rendered["plugins"][0]["strict"] is False


@settings(suppress_health_check=TMP)
@given(names=st.sets(NAMES, max_size=5))
def test_a_written_manifest_always_checks_clean_and_any_tree_change_makes_it_stale(
    tmp_path_factory: pytest.TempPathFactory, names: set[str]
) -> None:
    root = tmp_path_factory.mktemp("repo")
    for name in names:
        _skill(root, f"domains/{name}")
    _manifest(root)
    (root / MANIFEST_PATH).write_text(render_manifest(root))

    assert check_manifest(root) == []

    _skill(root, "domains/zz-added")
    stale = check_manifest(root)
    assert len(stale) == 1
    assert MANIFEST_PATH.as_posix() in stale[0]
    assert "--write" in stale[0]


def test_a_hand_edited_skill_list_is_reported(tmp_path: Path) -> None:
    _skill(tmp_path, "domains/kernel-performance")
    _manifest(tmp_path)
    (tmp_path / MANIFEST_PATH).write_text(render_manifest(tmp_path))
    manifest = json.loads((tmp_path / MANIFEST_PATH).read_text())
    manifest["plugins"][0]["skills"].append("./domains/ghost")
    _manifest(tmp_path, manifest)

    assert len(check_manifest(tmp_path)) == 1


@pytest.mark.parametrize(
    ("edit", "mention"),
    [
        (lambda e: e.update(version=""), "explicit `version`"),
        (lambda e: e.pop("version"), "explicit `version`"),
        (lambda e: e.update(source="./"), "`source` must be `./skills`"),
        (lambda e: e.update(name="other"), "exactly one entry"),
    ],
    ids=["empty-version", "no-version", "wrong-source", "wrong-name"],
)
def test_a_manifest_entry_the_generator_cannot_trust_is_reported_by_name(
    tmp_path: Path, edit: Callable[[dict[str, object]], object], mention: str
) -> None:
    manifest = json.loads(json.dumps(MANIFEST))
    edit(manifest["plugins"][0])
    _manifest(tmp_path, manifest)

    findings = check_manifest(tmp_path)

    assert len(findings) == 1
    assert MANIFEST_PATH.as_posix() in findings[0]
    assert mention in findings[0]


def test_a_missing_or_malformed_manifest_is_reported(tmp_path: Path) -> None:
    assert "missing" in check_manifest(tmp_path)[0]
    (tmp_path / MANIFEST_PATH).parent.mkdir(parents=True)
    (tmp_path / MANIFEST_PATH).write_text("{")
    assert "not valid JSON" in check_manifest(tmp_path)[0]
    with pytest.raises(ManifestError):
        render_manifest(tmp_path)


def test_a_skill_whose_name_differs_from_its_folder_is_reported(tmp_path: Path) -> None:
    _skill(tmp_path, "domains/kernel-performance", name="kernel-perf")
    _skill(tmp_path, "domains/serving-systems")

    messages = _messages(tmp_path)

    assert len(messages) == 1
    assert "domains/kernel-performance/SKILL.md" in messages[0]
    assert "must equal its folder name" in messages[0]


@pytest.mark.parametrize("name", ["Upper", "under_score", "-lead", "trail-", "a" * 65, "a--b"])
def test_a_name_outside_the_skill_rules_is_reported(tmp_path: Path, name: str) -> None:
    _skill(tmp_path, f"search/{name}")

    assert any("lowercase letters, digits and hyphens" in m for m in _messages(tmp_path))


@settings(suppress_health_check=TMP)
@given(name=NAMES)
def test_every_well_formed_name_matching_its_folder_passes(
    tmp_path_factory: pytest.TempPathFactory, name: str
) -> None:
    root = tmp_path_factory.mktemp("tree")
    _skill(root, f"search/{name}")
    assert _messages(root) == []


@settings(suppress_health_check=TMP)
@example(description="x" * 1025)
@given(description=st.text(alphabet="abc ", min_size=1025, max_size=1100).filter(str.strip))
def test_a_description_over_the_limit_is_reported(
    tmp_path_factory: pytest.TempPathFactory, description: str
) -> None:
    root = tmp_path_factory.mktemp("tree")
    _skill(root, "search/optimize", description=description)
    messages = _messages(root)
    assert len(messages) == 1
    assert "the limit is 1024" in messages[0]


def test_a_description_at_the_limit_and_a_missing_one_are_distinguished(tmp_path: Path) -> None:
    _skill(tmp_path, "search/at-limit", description="x" * 1024)
    folder = tmp_path / "skills/search/no-description"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: no-description\n---\nbody\n")

    messages = _messages(tmp_path)

    assert len(messages) == 1
    assert "no-description/SKILL.md" in messages[0]
    assert "`description`" in messages[0]


SPEC_OPTIONAL = (
    "license: Apache-2.0\n"
    "compatibility: Needs git.\n"
    "allowed-tools: Bash(git:*) Read\n"
    "metadata:\n  author: example-org\n  version: 1\n"
)


def test_every_optional_field_the_specification_defines_is_accepted(tmp_path: Path) -> None:
    _skill_with_fields(tmp_path, "search/optimize", SPEC_OPTIONAL)

    assert _messages(tmp_path) == []


@settings(suppress_health_check=TMP)
@given(field=st.from_regex(r"\A[a-z][a-z-]{0,12}\Z").filter(lambda f: f not in SPEC_FIELDS))
def test_a_field_outside_the_specification_is_reported_by_name(
    tmp_path_factory: pytest.TempPathFactory, field: str
) -> None:
    root = tmp_path_factory.mktemp("tree")
    _skill_with_fields(root, "search/optimize", f"{field}: x\n")

    messages = _messages(root)

    assert len(messages) == 1
    assert f"`{field}`" in messages[0]


def test_a_vendored_pack_may_use_fields_the_specification_lacks(tmp_path: Path) -> None:
    _skill_with_fields(tmp_path, "domains/pack/skills/pack-runtime", "argument-hint: x\n")
    (tmp_path / "skills/domains/pack/update.sh").write_text("#!/bin/sh\n")

    assert _messages(tmp_path) == []


@pytest.mark.parametrize(("length", "findings"), [(1, 0), (500, 0), (501, 1)])
def test_compatibility_is_limited_to_500_characters(
    tmp_path: Path, length: int, findings: int
) -> None:
    _skill_with_fields(tmp_path, "search/optimize", f"compatibility: {'x' * length}\n")

    assert len(_messages(tmp_path)) == findings


def test_metadata_must_be_a_mapping(tmp_path: Path) -> None:
    _skill_with_fields(tmp_path, "search/optimize", "metadata: plain\n")

    messages = _messages(tmp_path)

    assert len(messages) == 1
    assert "`metadata` must be a mapping" in messages[0]


def test_two_skills_with_one_name_are_reported_once_at_the_later_path(tmp_path: Path) -> None:
    _skill(tmp_path, "domains/pack-a/same")
    _skill(tmp_path, "search/same")
    _skill(tmp_path, "search/other")

    messages = _messages(tmp_path)

    assert len(messages) == 1
    assert "search/same/SKILL.md" in messages[0]
    assert "`./domains/pack-a/same`" in messages[0]


def test_unreadable_frontmatter_is_reported_with_the_path(tmp_path: Path) -> None:
    folder = tmp_path / "skills/search/bare"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("no frontmatter\n")

    messages = _messages(tmp_path)

    assert len(messages) == 1
    assert "search/bare/SKILL.md" in messages[0]
    assert "unreadable frontmatter" in messages[0]


@pytest.mark.parametrize("tool", VIBESYS_TOOL_NAMES)
def test_a_domain_skill_naming_a_vibesys_tool_is_reported_with_its_line(
    tmp_path: Path, tool: str
) -> None:
    _skill(tmp_path, "domains/kernel-performance", body=f"first\nCall `{tool}` now.\n")
    reference = tmp_path / "skills/domains/kernel-performance/references/notes.md"
    reference.parent.mkdir()
    reference.write_text(f"{tool}\n")

    messages = _messages(tmp_path)

    assert any("kernel-performance/SKILL.md:6" in m and tool in m for m in messages)
    assert any("references/notes.md:1" in m for m in messages)


@pytest.mark.parametrize("tree", ["search/optimize", "domains/pack/skills/pack-runtime"])
def test_search_skills_and_vendored_packs_may_name_any_tool(tmp_path: Path, tree: str) -> None:
    _skill(tmp_path, tree, body=f"Call `{VIBESYS_TOOL_NAMES[0]}`.\n")
    (tmp_path / "skills/domains/pack").mkdir(parents=True, exist_ok=True)
    (tmp_path / "skills/domains/pack/update.sh").write_text("#!/bin/sh\n")

    assert _messages(tmp_path) == []


def test_a_pack_without_an_update_script_is_held_to_the_tool_rule(tmp_path: Path) -> None:
    _skill(tmp_path, "domains/pack/skills/pack-runtime", body=f"Call `{VIBESYS_TOOL_NAMES[1]}`.\n")

    assert len(_messages(tmp_path)) == 1


def test_the_tool_name_list_matches_the_product_constants() -> None:
    assert set(VIBESYS_TOOL_NAMES) == {SUBMIT_TOOL, VALIDATE_WAIT_TOOL, READ_QUEUE_STATUS_TOOL}


def test_the_committed_manifest_and_every_published_skill_pass() -> None:
    assert check_manifest(REPOSITORY) == []
    assert _messages(REPOSITORY) == []
