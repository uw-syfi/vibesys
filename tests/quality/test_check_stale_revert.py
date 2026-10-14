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
from scripts.check_stale_revert import stale_files

from vs_project.api import run_git
from vs_sim.api.testing import examples

_TRAILER = "\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


def _git(root: Path, *args: str) -> str:
    result = run_git(list(args), cwd=root, text=True)
    result.check_returncode()
    return result.stdout.strip()


def _commit(root: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message + _TRAILER)
    return _git(root, "rev-parse", "HEAD")


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
