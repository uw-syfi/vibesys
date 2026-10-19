"""The stale-revert check flags exactly the files a change sets back to an older version.

Each case builds a small Git history in a temporary repository: a base branch that
moves one file through several versions, and a change branched from it that writes
that file and touches another.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from scripts.check_stale_revert import (
    GIT_COMMAND_TIMEOUT_SECONDS,
    GIT_LOW_SPEED_LIMIT,
    GIT_LOW_SPEED_SECONDS,
    bounded_git_argv,
    rewritten_append_only,
    stale_files,
)
from tests.support import run_test_command
from tests.support.real_git import REAL_GIT

from vs_sim.api.testing import examples

_TRAILER = "\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


def _git(root: Path, *args: str) -> str:
    return REAL_GIT.run_text(list(args), cwd=root).check().stdout.strip()


def _commit(root: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message + _TRAILER)
    return _git(root, "rev-parse", "HEAD")


def _commit_staged_pin(root: Path, message: str) -> None:
    """Commit an index-only gitlink as plain Git does.

    The product Git policy sets ``diff.ignoreSubmodules=all``, under which Git sees a
    pin whose directory is absent as "no changes added to commit".
    """
    run_test_command(["git", "commit", "-q", "-m", message + _TRAILER], cwd=root, check=True)


def _repository(root: Path) -> None:
    _git(root, "init", "-q", "--initial-branch=main")
    _git(root, "config", "user.name", "Stale revert fixture")
    _git(root, "config", "user.email", "stale@example.invalid")


@settings(max_examples=examples(pr=6, nightly=200), deadline=None)
@given(
    versions=st.integers(1, 4),
    data=st.data(),
)
def test_a_change_is_flagged_exactly_when_it_sets_a_file_back_to_an_older_version(
    versions: int, data: st.DataObject
) -> None:
    branch_point = data.draw(st.integers(0, versions), label="branch_point")
    later = data.draw(st.integers(0, 2), label="later main commits")
    written = data.draw(st.integers(-1, versions), label="version the change writes (-1: new)")
    declaration = data.draw(
        st.sampled_from(
            [None, "This reverts commit {sha}.", "Reland #{number}", "Restores #{number}"]
        ),
        label="how the change declares the undo",
    )
    contents = [f"version {index}\n" for index in range(versions + 1)]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _repository(root)
        commits = [
            _commit(root, {"f.txt": text}, f"main: f.txt version {index} (#{100 + index})")
            for index, text in enumerate(contents[: branch_point + 1])
        ]
        _git(root, "branch", "change")
        commits.extend(
            _commit(root, {"f.txt": contents[index]}, f"main: version {index} (#{100 + index})")
            for index in range(branch_point + 1, versions + 1)
        )
        for index in range(later):
            _commit(root, {f"later{index}.txt": "later\n"}, f"main: later {index}")
        _git(root, "checkout", "-q", "change")
        text = "the change's own text\n" if written < 0 else contents[written]
        undone = written + 1 if 0 <= written < branch_point else None
        message = "change: write f.txt"
        if declaration is not None and undone is not None:
            message += "\n\n" + declaration.format(sha=commits[undone], number=100 + undone)
        _commit(root, {"f.txt": text, "g.txt": "own\n"}, message)

        found = stale_files(root, "main")

    if undone is not None and declaration is None:
        assert [(stale.path, stale.undone) for stale in found] == [("f.txt", commits[undone])]
    else:
        assert found == []


def test_a_change_carrying_stale_copies_is_flagged_like_2343() -> None:
    """#2343: one commit fixed the server and carried pre-merge copies of other PRs' files.

    It was branched after #2327 and #2331 landed, and its commit still held the versions
    of their files from before them, and lacked the test file #2327 created. Its own fix
    and new test are not flagged. Dropping #2327's test file is, because the change also
    sets #2327's other file back.
    """
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _repository(root)
        _commit(
            root,
            {
                "src/headless/render.py": "def render(): ...\n",
                "libs/vs-faults/src/vs_faults/plan.py": "faults = []\n",
                "src/server/runtime.py": "drain when detached\n",
            },
            "base",
        )
        sanitizer = _commit(
            root,
            {
                "src/headless/render.py": "def render(): ...\ndef escape(): ...\n",
                "tests/headless/test_render_untrusted_text.py": "def test(): ...\n",
            },
            "fix(headless): show control characters in run text (#2327)",
        )
        faults = _commit(
            root,
            {"libs/vs-faults/src/vs_faults/plan.py": "faults = ['pending-forever']\n"},
            "feat(vs-faults): add pending-forever (#2331)",
        )
        _git(root, "checkout", "-q", "-b", "server2")
        _git(root, "rm", "-q", "tests/headless/test_render_untrusted_text.py")
        _commit(
            root,
            {
                "src/headless/render.py": "def render(): ...\n",
                "libs/vs-faults/src/vs_faults/plan.py": "faults = []\n",
                "src/server/runtime.py": "drain on every exit\n",
                "tests/server/test_runtime_on_sim.py": "def test(): ...\n",
            },
            "fix(server): let every client read why the run ended (#2343)",
        )

        found = stale_files(root, "main")

    assert {stale.path: stale.undone for stale in found} == {
        "libs/vs-faults/src/vs_faults/plan.py": faults,
        "src/headless/render.py": sanitizer,
        "tests/headless/test_render_untrusted_text.py": sanitizer,
    }


def test_deleting_a_recent_file_alone_is_a_refactor_not_a_stale_revert() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _repository(root)
        _commit(root, {"a.py": "a\n"}, "base")
        _commit(root, {"helper.py": "helper\n", "a.py": "a uses helper\n"}, "feat: helper (#7)")
        _git(root, "checkout", "-q", "-b", "refactor")
        _git(root, "rm", "-q", "helper.py")
        _commit(root, {"a.py": "a inlines helper\n"}, "refactor: inline the helper")

        assert stale_files(root, "main") == []


def test_moving_a_file_back_after_a_rename_passes_when_it_names_the_rename() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _repository(root)
        _commit(root, {"old/name.py": "body\n"}, "base")
        (root / "new").mkdir()
        _git(root, "mv", "old/name.py", "new/name.py")
        _commit(root, {}, "refactor: move name.py (#9)")
        _git(root, "checkout", "-q", "-b", "back")
        (root / "old").mkdir(exist_ok=True)
        _git(root, "mv", "new/name.py", "old/name.py")
        _commit(root, {}, "refactor: move name.py back")
        undeclared = stale_files(root, "main")
        _git(root, "commit", "-q", "--amend", "-m", "refactor: move name.py back\n\nRestores #9")

        declared = stale_files(root, "main")

    # Both halves of the move undo #9: the file put back, and the file it created removed.
    assert [stale.path for stale in undeclared] == ["new/name.py", "old/name.py"]
    assert declared == []


def _numbered(prefix: str, count: int) -> str:
    return "".join(f"{prefix} line {index}\n" for index in range(count))


def _recent_addition(root: Path, before: str, after: str) -> str:
    """A base with ``before``, then a commit (#50) that makes ``mod.py`` ``after``; its sha."""
    _repository(root)
    _commit(root, {"mod.py": before, "other.py": "other\n"}, "base")
    sha = _commit(root, {"mod.py": after}, "feat: the addition (#50)")
    _git(root, "checkout", "-q", "-b", "change")
    return sha


@settings(max_examples=examples(pr=6, nightly=100), deadline=None)
@given(
    added=st.integers(10, 30),
    own_edit=st.booleans(),
    declaration=st.sampled_from(
        [None, "Reverts #50", "Reland #50", "Restores #50", "Reverts o/r#50"]
    ),
)
def test_a_stale_copy_of_a_file_that_also_carries_an_edit_is_flagged_unless_declared(
    added: int, *, own_edit: bool, declaration: str | None
) -> None:
    """The old-worktree mistake: the file lacks a recent addition, plus one intended edit."""
    before = "header\n" + _numbered("body", 5)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        sha = _recent_addition(root, before, before + _numbered("added", added))
        stale = ("header edited\n" if own_edit else "header\n") + _numbered("body", 5)
        message = "change: edit the header" + (f"\n\n{declaration}" if declaration else "")
        _commit(root, {"mod.py": stale}, message)

        found = stale_files(root, "main")

    if declaration is None:
        # With no edit of its own the file is an exact old version: the whole-file check's catch.
        lines = added if own_edit else 0
        assert [(s.path, s.undone, s.lines) for s in found] == [("mod.py", sha, lines)]
    else:
        assert found == []


def test_a_change_that_puts_back_the_lines_a_recent_commit_replaced_is_flagged() -> None:
    old = "".join(f"old {index}\n" for index in range(6))
    new = "".join(f"new {index}\n" for index in range(6))
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        sha = _recent_addition(root, "top\n" + old, "top\n" + new)
        _commit(root, {"mod.py": "top edited\n" + old}, "change: edit the top")

        found = stale_files(root, "main")

    assert [(s.path, s.undone) for s in found] == [("mod.py", sha)]


@settings(max_examples=examples(pr=6, nightly=100), deadline=None)
@given(added=st.integers(5, 30), replaced=st.integers(1, 30))
def test_rewriting_a_recently_added_block_with_new_code_is_not_an_undo(
    added: int, replaced: int
) -> None:
    before = "header\n"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _recent_addition(root, before, before + _numbered("added", added))
        _commit(
            root, {"mod.py": before + _numbered("rewritten", added + replaced)}, "change: rewrite"
        )

        assert stale_files(root, "main") == []


def test_loose_declarations_do_not_exempt_a_stale_copy() -> None:
    before = "header\n"
    loose = [
        "I restore the addition from #50",
        "This reverts commit " + "a" * 7,
        "Please revert #50 and reland it",
        "unrestores #50",
    ]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _recent_addition(root, before, before + _numbered("added", 12))
        _git(root, "branch", "base-of-change")
        for text in loose:
            _git(root, "checkout", "-q", "-B", "attempt", "base-of-change")
            _commit(root, {"mod.py": before}, f"change: drop it\n\n{text}")

            assert [s.path for s in stale_files(root, "main")] == ["mod.py"], text


@settings(max_examples=examples(pr=6, nightly=100), deadline=None)
@given(
    lines=st.lists(st.sampled_from(["7 aa", "8 bb", "9 cc", "# note"]), min_size=1, max_size=4),
    appended=st.lists(st.sampled_from(["10 dd", "11 ee"]), max_size=2),
    edit=st.sampled_from(["append", "edit", "drop", "delete"]),
    marked=st.booleans(),
    data=st.data(),
)
def test_a_change_to_an_append_only_file_is_flagged_unless_it_only_appends(
    *, lines: list[str], appended: list[str], edit: str, marked: bool, data: st.DataObject
) -> None:
    header = ["# append-only: add lines at the end"] if marked else ["# pinned versions"]
    before = [*header, *lines]
    after = [*before, *appended]
    if edit in {"edit", "drop"}:
        index = data.draw(st.integers(0, len(before) - 1), label="line index")
        after[index : index + 1] = [after[index] + " changed"] if edit == "edit" else []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _repository(root)
        _commit(root, {"golden.txt": "\n".join(before) + "\n", "other.txt": "x\n"}, "base")
        _git(root, "checkout", "-q", "-b", "change")
        if edit == "delete":
            _git(root, "rm", "-q", "golden.txt")
            _commit(root, {"other.txt": "y\n"}, "change: drop the golden")
        else:
            golden = "\n".join(after) + "\n"
            _commit(root, {"golden.txt": golden, "other.txt": "y\n"}, "change: write the golden")

        found = rewritten_append_only(root, "main")

    assert found == (["golden.txt"] if marked and edit != "append" else [])


def test_bumping_a_submodule_pin_is_not_a_line_edit() -> None:
    """A gitlink holds a commit id, not lines, and its commit is absent from the checkout.

    CI's checkout has no submodule objects, so blaming the changed pin failed the whole
    check (#2987 bumped seven example submodules).
    """
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _repository(root)
        _commit(root, {"README.md": "base\n"}, "base")
        for index, pin in enumerate(("1" * 40, "2" * 40)):
            _git(root, "update-index", "--add", "--cacheinfo", f"160000,{pin},examples/sub")
            _commit_staged_pin(root, f"main: pin {index}")
        _git(root, "branch", "change")
        _git(root, "checkout", "-q", "change")
        _git(root, "update-index", "--cacheinfo", f"160000,{'3' * 40},examples/sub")
        _commit_staged_pin(root, "change: bump the pin")

        assert stale_files(root, "main") == []


@given(args=st.lists(st.text(min_size=1, max_size=12), max_size=6))
def test_every_git_command_carries_git_low_speed_abort_before_its_subcommand(
    args: list[str],
) -> None:
    argv = bounded_git_argv(*args)

    assert argv[0] == "git"
    assert argv[1:5] == [
        "-c",
        f"http.lowSpeedLimit={GIT_LOW_SPEED_LIMIT}",
        "-c",
        f"http.lowSpeedTime={GIT_LOW_SPEED_SECONDS}",
    ]
    assert argv[5:] == args


def test_the_per_command_bound_fits_inside_the_five_minute_job_limit() -> None:
    assert GIT_COMMAND_TIMEOUT_SECONDS < 5 * 60
