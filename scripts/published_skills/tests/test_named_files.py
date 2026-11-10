"""Contracts for the search skills' named-file check and the minimum vibesys-cli version check.

Each case builds a throwaway tree in a temporary directory, with a violating and a compliant
input per rule, so a check that matched nothing would fail too.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from scripts.published_skills import MIN_CLI_VERSION, check_min_cli_version, check_named_files
from scripts.published_skills.__main__ import REPOSITORY

NAME = st.from_regex(r"[a-z][a-z0-9_-]{0,8}\.(md|py|sh|txt)", fullmatch=True)
FOLDER = st.sampled_from(["references", "scripts", "assets", "references/harness"])


def _write(root: Path, relative: str, text: str = "x\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _skill(root: Path, name: str, body: str) -> None:
    _write(root, f"skills/search/{name}/SKILL.md", f"---\nname: {name}\n---\n{body}")


@given(
    folder=FOLDER,
    name=NAME,
    style=st.sampled_from(["{p}", "`{p}`", "[x]({p})", 'sh "${{CLAUDE_SKILL_DIR}}/{p}"']),
)
def test_a_named_file_is_reported_exactly_when_it_is_missing(
    folder: str, name: str, style: str
) -> None:
    relative = f"{folder}/{name}"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _skill(root, "demo", "intro\n" + style.format(p=relative) + "\n")
        missing = [str(finding) for finding in check_named_files(root)]
        _write(root, f"skills/search/demo/{relative}")
        present = check_named_files(root)
    assert present == []
    assert len(missing) == 1
    assert missing[0].startswith("skills/search/demo/SKILL.md:5: ")
    assert f"`{relative}`" in missing[0]


@pytest.mark.regression
@given(name=NAME)
@example(name="cli-setup.md")
def test_a_reference_file_deleted_from_a_skill_is_reported_where_another_skill_names_it(
    name: str,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _skill(root, "optimize", "body\n")
        _write(root, f"skills/search/create-task/references/{name}")
        _skill(root, "create-task", f"Follow `../optimize/references/{name}`.\n")
        findings = [str(finding) for finding in check_named_files(root)]
    assert len(findings) == 1
    assert findings[0].startswith("skills/search/create-task/SKILL.md:4: ")


def test_placeholders_repository_paths_and_domain_skills_are_not_checked(tmp_path: Path) -> None:
    _skill(
        tmp_path,
        "demo",
        "Read `references/harness/<harness>.md`.\n"
        "The repository's skills/search/other/scripts/x.py and cli/scripts/y.sh.\n",
    )
    _write(tmp_path, "skills/domains/kernel/SKILL.md", "---\nname: kernel\n---\nreferences/no.md\n")
    assert check_named_files(tmp_path) == []


def test_files_below_the_skill_are_scanned_and_resolved_from_the_skill_folder(
    tmp_path: Path,
) -> None:
    _skill(tmp_path, "demo", "body\n")
    _write(tmp_path, "skills/search/demo/references/a.md", "See scripts/run.sh.\n")
    assert [f.path for f in check_named_files(tmp_path)] == ["skills/search/demo/references/a.md:1"]
    _write(tmp_path, "skills/search/demo/scripts/run.sh")
    assert check_named_files(tmp_path) == []


RELEASE = st.builds(
    "{}.{}.{}".format, st.integers(0, 10**6), st.integers(0, 10**6), st.integers(0, 10**6)
)


@given(version=RELEASE)
def test_a_release_version_and_a_newline_pass(version: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp), MIN_CLI_VERSION, f"{version}\n")
        assert check_min_cli_version(Path(tmp)) == []


@given(text=st.text(alphabet="0123456789.-vrc \n", max_size=10))
@example(text="0.1.0-rc.1\n")
@example(text="v0.1.0\n")
@example(text="0.1.0")
@example(text="01.1.0\n")
def test_anything_else_in_the_version_file_is_reported(text: str) -> None:
    if re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\n", text):
        return
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp), MIN_CLI_VERSION, text)
        findings = check_min_cli_version(Path(tmp))
    assert [finding.path for finding in findings] == [MIN_CLI_VERSION]


def test_a_missing_version_file_is_reported(tmp_path: Path) -> None:
    assert [finding.path for finding in check_min_cli_version(tmp_path)] == [MIN_CLI_VERSION]


def test_the_committed_skills_name_only_existing_files_and_a_valid_minimum() -> None:
    assert check_named_files(REPOSITORY) == []
    assert check_min_cli_version(REPOSITORY) == []
