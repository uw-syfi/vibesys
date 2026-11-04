"""The example-pointer check refuses a pin moved without an ``Example-Bump:`` line naming it.

The parsing is checked over generated diffs and messages, and the command over a fake git; the
situation of #3568 in a real repository is `tests/e2e/test_check_example_pointers_e2e.py`.
"""

from __future__ import annotations

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

if TYPE_CHECKING:
    from pathlib import Path

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
    assert not is_example_repository("third_party/serving-repos/vllm")


class _FakeGit:
    """Answers the three git subcommands the check runs from a diff and a message, and records them."""

    def __init__(self, raw_diff: str, message: str) -> None:
        self.raw_diff = raw_diff
        self.message = message
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        match args[0]:
            case "merge-base":
                return "b" * 40 + "\n"
            case "diff":
                return self.raw_diff
            case "log":
                return self.message
        message = f"unexpected git {args}"
        raise AssertionError(message)


def test_the_check_asks_git_for_gitlinks_between_the_merge_base_and_head() -> None:
    git = _FakeGit(_raw([(GITLINK_MODE, GITLINK_MODE, "examples/f/repositories/n")]), "m")

    assert pointer_changes(git, "origin/main", "HEAD") == (["examples/f/repositories/n"], "m")
    diff = next(call for call in git.calls if call[0] == "diff")
    assert "--ignore-submodules=none" in diff, "the product Git policy hides gitlinks otherwise"
    assert diff[-4:] == ("b" * 40, "HEAD", "--", "examples")
    assert ("log", "--format=%B", f"{'b' * 40}..HEAD") in git.calls


def test_the_command_names_the_pin_and_the_fix_and_writes_the_ci_output(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    pin = "examples/family/repositories/masa"
    output = tmp_path / "github-output"
    arguments = ["--base-ref", "origin/main", "--github-output", str(output)]

    assert main(arguments, git=_FakeGit("", "an ordinary change")) == 0
    assert output.read_text() == "example_pointers=false\n"

    moved = _raw([(GITLINK_MODE, GITLINK_MODE, pin)])
    assert main(arguments, git=_FakeGit(moved, "record the worktree's commit")) == 1
    report = capsys.readouterr().out
    assert pin in report
    assert f"Example-Bump: {pin}" in report
    assert output.read_text().endswith("example_pointers=true\n")

    assert main(arguments, git=_FakeGit(moved, f"bump masa\n\nExample-Bump: {pin}\n")) == 0
