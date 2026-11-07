"""Repository shell scripts edit in place only in the form GNU and BSD (macOS) sed share.

``sed -i EXPR FILE`` is GNU-only: BSD sed reads the next word as the backup suffix, so
``sed -i -E ...`` fails with "invalid command code" and ``sed -i -e ...`` leaves ``FILE-e``
behind. ``sed -i.bak ...`` (the suffix attached) or a write to a new file and ``mv`` works on both.
Every script here runs on a contributor's Mac as well as on Linux, so a bare ``-i`` is a failure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_ROOTS = ("scripts", "orchestrator", "cli", "gateway", "packaging", "support", ".githooks")
SKIPPED_DIRECTORIES = {".venv", "node_modules", "target", "gen", "repositories"}

# `sed`, any options, then an `-i` that takes the next word: a short-option cluster ending in `i`
# with nothing attached, or the long form.
BARE_IN_PLACE = re.compile(r"\bsed\s+(?:\S+\s+)*?(?:-[A-Za-z]*i|--in-place)(?=\s|$)")


def bare_in_place_edits(text: str) -> list[str]:
    """The lines of a shell script that run `sed` with an in-place flag that is not portable."""
    return [
        line.strip()
        for line in text.splitlines()
        if not line.lstrip().startswith("#") and BARE_IN_PLACE.search(line)
    ]


def _scripts() -> list[Path]:
    found: list[Path] = []
    for name in SCRIPT_ROOTS:
        root = REPO_ROOT / name
        if root.is_dir():
            found.extend(
                path
                for path in root.rglob("*")
                if path.is_file()
                and (path.suffix == ".sh" or path.parent.name == ".githooks")
                and not SKIPPED_DIRECTORIES & set(path.relative_to(REPO_ROOT).parts)
            )
    return sorted(found)


@pytest.mark.parametrize(
    "line",
    [
        "sed -i -E 's/a/b/' file",
        "sed -i -e 's/a/b/' file",
        'sed -Ei "s/a/b/" file',
        "sed -n -i 's/a/b/p' file",
        "sed --in-place 's/a/b/' file",
        "  find . -exec sed -i 's/a/b/' {} +",
    ],
)
def test_a_bare_in_place_flag_is_flagged(line: str) -> None:
    assert bare_in_place_edits(line) == [line.strip()]


@pytest.mark.parametrize(
    "line",
    [
        "sed -i.bak -E 's/a/b/' file",
        "sed -E 's/a/b/' file > file.new",
        "sed 's/-i /x/' file",
        "# sed -i takes a backup suffix on BSD",
        "echo hello",
    ],
)
def test_a_portable_or_unrelated_line_is_not_flagged(line: str) -> None:
    assert bare_in_place_edits(line) == []


def test_the_scan_reaches_the_scripts_it_guards() -> None:
    names = {path.name for path in _scripts()}
    assert {"generate.sh", "check_proto.sh", "pre-push"} <= names


def test_no_repository_script_edits_in_place_with_a_bare_flag() -> None:
    offenders = {
        str(path.relative_to(REPO_ROOT)): hits
        for path in _scripts()
        if (hits := bare_in_place_edits(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, "use `sed -i.bak ...; rm file.bak` (portable) or a copy and mv"
