"""The example-pointer check refuses a pin moved without an ``Example-Bump:`` line naming it.

The parsing is checked over generated diffs and messages; one case builds the situation of
#3568 in a temporary repository: a change that commits a worktree's checked-out submodule commit.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st
from scripts.check_example_pointers import (
    GITLINK_MODE,
    changed_pointers,
    declared_bumps,
    is_example_repository,
    main,
    pointer_changes,
    undeclared,
)
from tests.support import run_test_command
from tests.support.real_git import REAL_GIT

if TYPE_CHECKING:
    import pytest

_NAMES = st.text(st.sampled_from("abcxyz-"), min_size=1, max_size=6)
_PINS = st.builds(
    lambda family, name: f"examples/{family}/repositories/{name}",
    _NAMES,
    _NAMES,
)
_OTHERS = st.one_of(
    st.builds(lambda name: f"examples/{name}/repositories/{name}/src/a.py", _NAMES),
    st.builds(lambda name: f"examples/{name}/.vibesys/tasks/{name}", _NAMES),
    st.builds(lambda name: f"examples/{name}/repositories/{name}", _NAMES).map(lambda p: p + "/"),
    st.builds(lambda name: f"src/{name}/repositories/{name}", _NAMES),
)


def _raw(entries: list[tuple[str, str, str]]) -> str:
    """``git diff --raw -z`` output for ``(old mode, new mode, path)`` entries."""
    return "".join(f":{old} {new} {'1' * 40} {'2' * 40} M\0{path}\0" for old, new, path in entries)


@given(
    pins=st.lists(_PINS, max_size=4, unique=True),
    others=st.lists(_OTHERS, max_size=4, unique=True),
    files=st.lists(_PINS, max_size=3, unique=True),
)
def test_exactly_the_gitlinks_under_example_repositories_are_reported(
    pins: list[str], others: list[str], files: list[str]
) -> None:
    entries = [
        *[(GITLINK_MODE, GITLINK_MODE, path) for path in pins],
        *[(GITLINK_MODE, GITLINK_MODE, path) for path in others],
        *[("100644", "100644", path) for path in files if path not in pins],
    ]

    assert changed_pointers(_raw(entries)) == sorted(pins)


@given(
    changed=st.lists(_PINS, max_size=5, unique=True),
    named=st.sets(_PINS, max_size=5),
    noise=st.text(st.sampled_from("abc \n:#"), max_size=20),
)
def test_a_pin_passes_exactly_when_a_line_of_the_message_names_it(
    changed: list[str], named: set[str], noise: str
) -> None:
    message = noise + "\n" + "".join(f"Example-Bump: {path}\n" for path in sorted(named)) + noise

    assert undeclared(changed, message) == sorted(set(changed) - named)


@given(path=_PINS)
def test_a_mention_that_is_not_a_whole_line_trailer_declares_nothing(path: str) -> None:
    for message in (
        f"see Example-Bump: {path}",
        f"example-bump: {path}",
        f"Example-Bump {path}",
        f"Example-Bump: {path} and more",
    ):
        assert declared_bumps(message) == set()


def test_a_path_is_an_example_repository_only_at_the_one_depth() -> None:
    assert is_example_repository("examples/microservices/repositories/masa")
    assert not is_example_repository("examples/microservices/repositories/masa/sub")
    assert not is_example_repository("examples/microservices/repositories")
    assert not is_example_repository("resources/skills/serving-systems/repos/vllm")


def _git(root: Path, *args: str) -> str:
    return REAL_GIT.run_text(list(args), cwd=root).check().stdout.strip()


def _commit_pin(root: Path, pin: str, message: str) -> None:
    _git(
        root,
        "update-index",
        "--add",
        "--cacheinfo",
        f"{GITLINK_MODE},{pin},examples/family/repositories/masa",
    )
    # The product Git policy sets diff.ignoreSubmodules=all; plain git commits the index-only pin.
    run_test_command(["git", "commit", "-q", "-m", message], cwd=root, check=True)


def test_a_change_that_commits_the_checked_out_pin_fails_unless_it_declares_the_bump() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _git(root, "init", "-q", "--initial-branch=main")
        _git(root, "config", "user.name", "Pointer fixture")
        _git(root, "config", "user.email", "pointer@example.invalid")
        (root / "README.md").write_text("base\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "base")
        _commit_pin(root, "1" * 40, "main: pin the example")
        _git(root, "checkout", "-q", "-b", "change")
        (root / "README.md").write_text("change\n")
        _git(root, "commit", "-qam", "change: an ordinary edit")

        assert pointer_changes(root, "main") == ([], "change: an ordinary edit\n\n")

        _commit_pin(root, "2" * 40, "change: record the worktree's commit")
        changed, message = pointer_changes(root, "main")
        assert changed == ["examples/family/repositories/masa"]
        assert undeclared(changed, message) == changed

        _commit_pin(
            root,
            "3" * 40,
            "change: bump masa\n\nExample-Bump: examples/family/repositories/masa",
        )
        changed, message = pointer_changes(root, "main")
        assert undeclared(changed, message) == []


def test_the_command_names_the_pin_and_the_fix_and_writes_the_ci_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _git(root, "init", "-q", "--initial-branch=main")
        _git(root, "config", "user.name", "Pointer fixture")
        _git(root, "config", "user.email", "pointer@example.invalid")
        (root / "README.md").write_text("base\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "base")
        _git(root, "checkout", "-q", "-b", "change")
        output = root / "github-output"
        arguments = ["--base-ref", "main", "--github-output", str(output)]

        assert main(arguments, root=root) == 0
        assert output.read_text() == "example_pointers=false\n"

        _commit_pin(root, "2" * 40, "change: record the worktree's commit")
        assert main(arguments, root=root) == 1
        report = capsys.readouterr().out
        assert "examples/family/repositories/masa" in report
        assert "Example-Bump: examples/family/repositories/masa" in report
        assert output.read_text().endswith("example_pointers=true\n")

        _commit_pin(
            root,
            "3" * 40,
            "change: bump masa\n\nExample-Bump: examples/family/repositories/masa",
        )
        assert main(arguments, root=root) == 0
