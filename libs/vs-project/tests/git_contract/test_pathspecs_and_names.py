"""GitRepository contract: pathspec subset and name rules, compared with the oracle.

An implementation that answers pathspecs or name checks itself (instead of
asking Git) has to give Git's answer for the whole documented subset. Each case
asks the implementation under test and the oracle the same generated question
about the same directory.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.git_contract import Sandbox

from vs_project.api import GitCommandError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from tests.support.git_contract import RepositoryFactory

    from vs_project.api import GitRepository

_FILES = (
    "a.txt",
    "b.py",
    "dir/c.txt",
    "dir/sub/d.py",
    "dir/sub/deep/e.txt",
    "dir/.hidden",
    "other/f.txt",
    "x/node_modules/g.js",
    "node_modules/h.js",
    "dir.bak",
    "sp ace.txt",
    "é.txt",
)
_NEW_FILES = ("other/new.txt", "dir/sub/new.py")

_PATHSPECS = st.sampled_from(
    [
        ".",
        "dir",
        "dir/",
        "dir/sub",
        "./dir",
        "other",
        "a.txt",
        "missing",
        "dir.bak",
        "sp ace.txt",
        "é.txt",
        "dir/../other",
        "*.py",
        ":(literal)dir",
        ":(literal)dir/c.txt",
        ":(literal)é.txt",
        ":(glob)**/node_modules/**",
        ":(glob)**/node_modules",
        ":(glob)**/*.py",
        ":(glob)dir/*",
        ":(glob)dir/**",
        ":(glob)*.txt",
        ":(glob)?.txt",
        ":(glob)dir/*/d.py",
        ":(glob)**/sub/**/*.txt",
        ":(glob)dir/**/e.txt",
        ":(glob)[ab].txt",
        ":(exclude)dir",
        ":(exclude)dir/**",
        ":(exclude)*.py",
        ":(exclude)x",
        ":(exclude)other/f.txt",
        ":(exclude)node_modules/**",
        ":!dir",
    ]
)
_SPEC_LISTS = st.lists(_PATHSPECS, min_size=1, max_size=3)


def _populate(sandbox: Sandbox) -> None:
    """A history with the files above, then staged and unstaged edits across them."""
    sandbox.start({path: f"{path}\n" for path in _FILES})
    sandbox.write("b.py", "edited\n")
    sandbox.write("dir/sub/d.py", "edited\n")
    sandbox.write("x/node_modules/g.js", "edited\n")
    sandbox.write("dir/c.txt", "edited\n")
    sandbox.write("other/new.txt", "new\n")
    sandbox.write("dir/sub/new.py", "new\n")
    sandbox.delete("a.txt")
    sandbox.repo.stage_all(["dir/c.txt", "other/new.txt", "a.txt"])
    sandbox.write("dir/c.txt", "edited twice\n")
    sandbox.write("untracked.txt", "u\n")


@pytest.fixture
def populated(sandbox: Sandbox) -> Sandbox:
    _populate(sandbox)
    return sandbox


@pytest.fixture
def started(sandbox: Sandbox) -> Sandbox:
    sandbox.start({"a.txt": "1\n"})
    return sandbox


def _outcome(call: Callable[[], object]) -> object:
    """The result of ``call()``, or the exception type when the repository refused."""
    try:
        return call()
    except GitCommandError as error:
        return type(error)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=40)
@given(specs=_SPEC_LISTS)
def test_pathspec_reads_agree_with_the_oracle(
    populated: Sandbox, oracle_factory: RepositoryFactory, specs: list[str]
) -> None:
    # Reads only: one populated sandbox serves every example.
    oracle = oracle_factory(populated.root)
    repo = populated.repo

    assert _outcome(lambda: repo.tracked_changes_since_head(specs)) == _outcome(
        lambda: oracle.tracked_changes_since_head(specs)
    ), specs
    assert repo.has_staged_changes(specs) == oracle.has_staged_changes(specs), specs
    for spec in specs:
        assert _outcome(lambda spec=spec: repo.has_tracked_files(spec)) == _outcome(
            lambda spec=spec: oracle.has_tracked_files(spec)
        ), spec


def _staged_state(repo: GitRepository) -> object:
    """What the index holds, observed by committing it: the outcome and every file at ``HEAD``."""
    try:
        repo.commit("probe")
    except GitCommandError:
        return "nothing staged"
    return tuple(repo.read_blob("HEAD", path) for path in (*_FILES, *_NEW_FILES))


@settings(max_examples=12)
@given(specs=_SPEC_LISTS)
def test_unstage_agrees_with_the_oracle(
    factory: RepositoryFactory, oracle_factory: RepositoryFactory, specs: list[str]
) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        states = []
        for name, make in (("subject", factory), ("oracle", oracle_factory)):
            root = Path(scratch) / name
            root.mkdir()
            sandbox = Sandbox(root=root, factory=make)
            _populate(sandbox)
            sandbox.repo.stage_all(["."])
            outcome = _outcome(lambda repo=sandbox.repo: repo.unstage(specs))
            states.append((outcome, _staged_state(sandbox.repo)))

        assert states[0] == states[1], specs


def _fragments() -> st.SearchStrategy[str]:
    pieces = [
        "a",
        "b",
        "HEAD",
        "@",
        "@{",
        "{",
        "}",
        "-",
        ".",
        "..",
        "/",
        "//",
        ".lock",
        "refs/heads/",
        "~",
        "^",
        ":",
        "*",
        "?",
        "[",
        "\\",
        " ",
        "\x01",
        "\x7f",
        "é",
        "-x",
        "x.",
    ]
    return st.lists(st.sampled_from(pieces), max_size=6).map("".join)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=200)
@given(name=_fragments())
def test_name_rules_agree_with_the_oracle_on_assembled_names(
    sandbox: Sandbox, oracle_factory: RepositoryFactory, name: str
) -> None:
    oracle = oracle_factory(sandbox.root)
    repo = sandbox.repo

    assert repo.is_valid_branch_name(name) == oracle.is_valid_branch_name(name), name
    assert repo.is_valid_ref_name(name) == oracle.is_valid_ref_name(name), name
    full = f"refs/vibesys/{name}"
    assert repo.is_valid_ref_name(full) == oracle.is_valid_ref_name(full), full


# -- reading the same repository through two instances ---------------------------------------


def test_a_tag_with_a_branch_name_does_not_change_the_current_branch(sandbox: Sandbox) -> None:
    head = sandbox.start({"a.txt": "1\n"})
    sandbox.repo.update_ref("refs/tags/main", head)

    assert sandbox.repo.current_branch() == "main"


def test_a_directory_is_not_a_blob(sandbox: Sandbox) -> None:
    sandbox.start({"dir/a.txt": "1\n"})

    assert sandbox.repo.read_blob("HEAD", "dir") is None
    assert sandbox.repo.read_blob("HEAD", "dir/a.txt") == b"1\n"


def test_resetting_the_index_before_the_first_commit_is_a_command_error(sandbox: Sandbox) -> None:
    sandbox.repo.initialize(initial_branch="main")
    sandbox.repo.bind()

    with pytest.raises(GitCommandError):
        sandbox.repo.reset_index()


def test_non_ascii_paths_are_reported_as_they_are_named(sandbox: Sandbox) -> None:
    sandbox.start({"é.txt": "1\n", "plain.txt": "1\n"})
    sandbox.write("é.txt", "2\n")

    assert sandbox.repo.tracked_changes_since_head(["."]) == ("é.txt",)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=40)
@given(length=st.integers(min_value=1, max_value=40), unknown=st.booleans())
def test_revisions_resolve_like_the_oracle(
    started: Sandbox, oracle_factory: RepositoryFactory, length: int, *, unknown: bool
) -> None:
    sandbox = started
    head = sandbox.repo.head()
    assert head is not None
    revision = ("f" * length) if unknown else head[:length]
    oracle = oracle_factory(sandbox.root)

    assert sandbox.repo.resolve_commit(revision) == oracle.resolve_commit(revision)
    assert sandbox.repo.is_ancestor(revision, "HEAD") == oracle.is_ancestor(revision, "HEAD")


def _subjects(repo: GitRepository, limit: int) -> object:
    return _outcome(lambda: [(e.sha, e.subject) for e in repo.recent_subjects(limit)])


_LINES = st.sampled_from(["fix", "part two", "  indented", "trail  ", "\t", "", " ", "é"])


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=30)
@given(
    lines=st.lists(_LINES, min_size=1, max_size=5),
    limit=st.integers(min_value=0, max_value=4),
)
def test_subjects_read_like_the_oracle(
    started: Sandbox, oracle_factory: RepositoryFactory, lines: Sequence[str], limit: int
) -> None:
    sandbox = started
    message = "\n".join(lines)
    if not message.strip():
        return
    try:
        sandbox.repo.commit(message, allow_empty=True)
    except GitCommandError:
        return
    oracle = oracle_factory(sandbox.root)

    assert _subjects(sandbox.repo, limit) == _subjects(oracle, limit)
