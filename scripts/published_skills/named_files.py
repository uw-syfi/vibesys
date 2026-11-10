"""Files the search skills name, and the vibesys-cli version range they declare, must hold up.

A search skill tells an agent to open ``references/...``, run ``scripts/...`` or read
``assets/...`` relative to its own folder (or a sibling's, through ``../<skill>/``). Such a path
in prose, a code span or a fenced command is not a Markdown link, so ``check_doc_links`` does not
see it, and a deleted or renamed file would leave the agent following a dead instruction.

The ``setup`` skill holds the oldest vibesys-cli version the skills support, which its version
helper reads; a file that is missing or not a plain ``MAJOR.MINOR.PATCH`` would make every
version check fail at run time instead of here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from scripts.published_skills.findings import Finding

if TYPE_CHECKING:
    from pathlib import Path

SEARCH_TREE = "skills/search"
"""The first-party skills whose Markdown is checked; domain skills cite other roots."""

NAMED_PATH = re.compile(
    r"(?<![\w./{}$-])"  # not inside a longer path or a variable
    r"((?:\.\./[a-z0-9-]+/)?"  # optionally a sibling skill's folder
    r"(?:references|scripts|assets)/[A-Za-z0-9._/<>-]*[A-Za-z0-9_>])"
)
"""A skill-relative path; one holding a ``<placeholder>`` stands for a family and is skipped."""

SKILL_DIR_PLACEHOLDERS = ("${CLAUDE_SKILL_DIR}/", "<directory>/")
"""How the harness files spell "this skill's folder" before a path."""

MIN_CLI_VERSION = "skills/search/setup/assets/min-cli-version"
"""The oldest vibesys-cli version the skills support: the one source of that fact."""

RELEASE_VERSION = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")


def check_named_files(root: Path) -> list[Finding]:
    """One finding per skill-relative path a search skill names that does not exist."""
    findings: list[Finding] = []
    for skill_file in sorted((root / SEARCH_TREE).glob("*/SKILL.md")):
        folder = skill_file.parent
        for markdown in sorted(folder.rglob("*.md")):
            lines = markdown.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines, start=1):
                findings.extend(
                    Finding(
                        f"{markdown.relative_to(root).as_posix()}:{number}",
                        f"names `{named}`, which does not exist under "
                        f"`{folder.relative_to(root).as_posix()}`; "
                        "fix the path or restore the file",
                    )
                    for named in NAMED_PATH.findall(_without_placeholders(line))
                    if "<" not in named and not (folder / named).exists()
                )
    return findings


def _without_placeholders(line: str) -> str:
    for placeholder in SKILL_DIR_PLACEHOLDERS:
        line = line.replace(placeholder, " ")
    return line


def check_min_cli_version(root: Path) -> list[Finding]:
    """A finding unless the minimum vibesys-cli version file holds one release version."""
    path = root / MIN_CLI_VERSION
    if not path.is_file():
        return [Finding(MIN_CLI_VERSION, "missing; it holds the oldest supported vibesys-cli")]
    text = path.read_text(encoding="utf-8")
    if text.endswith("\n") and RELEASE_VERSION.fullmatch(text[:-1]):
        return []
    return [
        Finding(
            MIN_CLI_VERSION,
            f"must hold one MAJOR.MINOR.PATCH version and a newline (no `v`, no pre-release), "
            f"found {text!r}",
        )
    ]
