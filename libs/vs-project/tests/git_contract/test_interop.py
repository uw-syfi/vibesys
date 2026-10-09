"""GitRepository contract: an implementation and plain ``git`` share one repository.

Agents run the real ``git`` in the workspace the framework tracks, so whatever an
implementation writes (index, refs, locks) must stay readable and writable by
Git, and what Git writes must be visible to the implementation at once. Each
example drives one directory with a random mix of three actors, the
implementation under test, the CLI oracle, and bare ``git`` commands (the
agent), then replays the same operations with the oracle alone in a second
directory. Both must end every step with the same index and ``HEAD`` tree.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_project.api import GitCommandError, run_git

if TYPE_CHECKING:
    from tests.support.git_contract import RepositoryFactory, Sandbox

    from vs_project.api import GitRepository

_PATHS = ("a.txt", "b.txt", "dir/c.txt")
_TEXTS = ("one\n", "two\n", "three\n")
_AGENT_IDENTITY = ["-c", "user.name=agent", "-c", "user.email=agent@example.invalid"]

type Actor = Literal["subject", "oracle", "agent"]


@dataclass(frozen=True)
class _Step:
    kind: Literal["write", "delete", "stage", "unstage", "reset_index", "commit", "update_ref"]
    actor: Actor = "subject"
    path: str = _PATHS[0]
    text: str = _TEXTS[0]
    ref: int = 0


_STEPS = st.builds(
    _Step,
    kind=st.sampled_from(
        [
            "write",
            "write",
            "delete",
            "stage",
            "stage",
            "unstage",
            "reset_index",
            "commit",
            "update_ref",
        ]
    ),
    actor=st.sampled_from(["subject", "oracle", "agent"]),
    path=st.sampled_from(_PATHS),
    text=st.sampled_from(_TEXTS),
    ref=st.integers(min_value=0, max_value=2),
)


def _agent(root: Path, *args: str) -> bool:
    """Run bare ``git`` the way an agent does; whether it succeeded."""
    return run_git([*_AGENT_IDENTITY, *args], cwd=root).returncode == 0


def _run(step: _Step, root: Path, repos: dict[Actor, GitRepository], counter: int) -> bool:
    """Apply ``step`` with its actor; ``False`` when the repository refused it."""
    path = root / step.path
    if step.kind == "write":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(step.text, encoding="utf-8")
        return True
    if step.kind == "delete":
        path.unlink(missing_ok=True)
        return True
    ref = f"refs/vibesys/run/candidates/c{step.ref}"
    if step.actor == "agent":
        agent_commands = {
            "stage": ["add", "-A", "--", "."],
            "unstage": ["reset", "-q", "HEAD", "--", step.path],
            "reset_index": ["reset", "-q", "--mixed", "HEAD"],
            "commit": ["commit", "-q", "-m", f"step {counter}"],
            "update_ref": ["update-ref", ref, "HEAD"],
        }
        return _agent(root, *agent_commands[step.kind])
    repo = repos[step.actor]
    try:
        if step.kind == "stage":
            repo.stage_all(["."])
        elif step.kind == "unstage":
            repo.unstage([step.path])
        elif step.kind == "reset_index":
            repo.reset_index()
        elif step.kind == "commit":
            repo.commit(f"step {counter}")
        else:
            head = repo.head()
            assert head is not None
            repo.update_ref(ref, head)
    except GitCommandError:
        return False
    return True


def _snapshot(root: Path) -> tuple[str, str, str]:
    """The index entries, the ``HEAD`` tree, and every ref name, as plain ``git`` reports them."""
    index = run_git(["ls-files", "--stage"], cwd=root).stdout.decode()
    tree = run_git(["rev-parse", "--verify", "--quiet", "HEAD^{tree}"], cwd=root).stdout.decode()
    refs = run_git(["for-each-ref", "--format=%(refname)"], cwd=root).stdout.decode()
    return index, tree, refs


def _assert_healthy(root: Path, step: _Step) -> None:
    fsck = run_git(["fsck", "--strict", "--no-dangling"], cwd=root)
    assert fsck.returncode == 0, (step, fsck.stderr)
    status = run_git(["status", "--porcelain"], cwd=root)
    assert status.returncode == 0, (step, status.stderr)
    assert not list((root / ".git").rglob("*.lock")), step


def _prepare(root: Path, make: RepositoryFactory) -> dict[Actor, GitRepository]:
    root.mkdir()
    repo = make(root)
    repo.initialize(initial_branch="main")
    repo.bind()
    (root / "a.txt").write_text("base\n", encoding="utf-8")
    repo.stage_all(["."])
    repo.commit("baseline")
    return {"subject": repo}


@settings(max_examples=10)
@given(steps=st.lists(_STEPS, min_size=1, max_size=8))
def test_alternating_implementations_and_git_agree_with_an_all_cli_run(
    factory: RepositoryFactory, oracle_factory: RepositoryFactory, steps: list[_Step]
) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        mixed_root = Path(scratch) / "mixed"
        pure_root = Path(scratch) / "pure"
        mixed = _prepare(mixed_root, factory)
        mixed["oracle"] = oracle_factory(mixed_root)
        mixed["oracle"].bind()
        mixed["subject"] = factory(mixed_root)
        mixed["subject"].bind()
        pure = _prepare(pure_root, oracle_factory)
        pure["oracle"] = pure["subject"]

        for counter, step in enumerate(steps):
            accepted = _run(step, mixed_root, mixed, counter)
            pure_step = _Step(step.kind, "oracle", step.path, step.text, step.ref)
            assert _run(pure_step, pure_root, pure, counter) == accepted, step
            assert _snapshot(mixed_root)[:2] == _snapshot(pure_root)[:2], step
            assert _snapshot(mixed_root)[2].splitlines() == _snapshot(pure_root)[2].splitlines()
            _assert_healthy(mixed_root, step)
            # What the implementation reads is what the oracle reads, step by step.
            subject, oracle = mixed["subject"], mixed["oracle"]
            assert subject.head() == oracle.head(), step
            assert subject.current_branch() == oracle.current_branch(), step
            assert subject.has_staged_changes() == oracle.has_staged_changes(), step


def test_a_held_index_lock_refuses_index_changes_and_leaves_the_index_alone(
    sandbox: Sandbox,
) -> None:
    sandbox.start({"a.txt": "1\n"})
    repo = sandbox.repo
    sandbox.write("a.txt", "2\n")
    sandbox.write("b.txt", "b\n")
    repo.stage_all(["."])
    git_dir = repo.bind().git_dir
    lock = git_dir / "index.lock"
    lock.write_text("held by another process", encoding="utf-8")
    try:
        with pytest.raises(GitCommandError):
            repo.unstage(["a.txt"])
        with pytest.raises(GitCommandError):
            repo.reset_index()
        assert repo.has_staged_changes(["a.txt"])
        assert repo.has_staged_changes(["b.txt"])
        assert lock.read_text(encoding="utf-8") == "held by another process"
    finally:
        lock.unlink()

    repo.unstage(["a.txt"])
    assert not repo.has_staged_changes(["a.txt"])
    assert repo.has_staged_changes(["b.txt"])


def test_a_held_ref_lock_refuses_the_update_and_leaves_the_ref_alone(sandbox: Sandbox) -> None:
    first = sandbox.start({"a.txt": "1\n"})
    sandbox.write("a.txt", "2\n")
    second = sandbox.commit_all("second")
    repo = sandbox.repo
    git_dir = repo.bind().git_dir
    ref = "refs/vibesys/run/candidates/x"
    repo.update_ref(ref, first)
    lock = git_dir / f"{ref}.lock"
    lock.write_text("held", encoding="utf-8")
    try:
        with pytest.raises(GitCommandError):
            repo.update_ref(ref, second)
        assert repo.has_ref_containing(first, "refs/vibesys/run/candidates/")
        assert not repo.has_ref_containing(second, "refs/vibesys/run/candidates/")
    finally:
        lock.unlink()

    repo.update_ref(ref, second)
    assert repo.has_ref_containing(second, "refs/vibesys/run/candidates/")


def test_history_with_merges_and_several_roots_reads_like_the_oracle(
    sandbox: Sandbox, oracle_factory: RepositoryFactory
) -> None:
    base = sandbox.start({"a.txt": "1\n"})
    root = sandbox.root
    assert _agent(root, "checkout", "-q", "-b", "side")
    sandbox.write("side.txt", "s\n")
    sandbox.commit_all("side work")
    assert _agent(root, "checkout", "-q", "main")
    sandbox.write("main.txt", "m\n")
    sandbox.commit_all("main work")
    assert _agent(root, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    # A second, unrelated root commit, merged in.
    empty_tree = (
        run_git(["hash-object", "-t", "tree", "/dev/null"], cwd=root).stdout.decode().strip()
    )
    orphan = (
        run_git([*_AGENT_IDENTITY, "commit-tree", empty_tree, "-m", "second root"], cwd=root)
        .stdout.decode()
        .strip()
    )
    assert _agent(root, "merge", "-q", "--allow-unrelated-histories", "-m", "join roots", orphan)
    oracle = oracle_factory(root)
    repo = sandbox.repo

    for limit in (1, 2, 3, 4, 10):
        assert repo.recent_subjects(limit) == oracle.recent_subjects(limit)
    assert repo.root_commit("HEAD") == oracle.root_commit("HEAD")
    assert repo.root_commit(base) == base
    assert repo.is_ancestor(base, "HEAD")
    assert repo.is_ancestor(orphan, "HEAD")
    assert not repo.is_ancestor("HEAD", orphan)


def test_an_index_rewrite_keeps_same_second_edits_visible_to_git(sandbox: Sandbox) -> None:
    """Git trusts an entry's file stat unless the entry is "racily clean".

    It judges that against the index file's timestamp, whole seconds only, so a
    file edited in the same second the entry was recorded, to the same size, is
    only noticed while the index is stamped no later than that second. An
    implementation that rewrites the index and stamps it later, without the
    precautions Git takes when it rewrites, hides such an edit from the next
    ``git add``. Timestamps are set by hand to recreate the situation without
    waiting: the entry is recorded at .1 of a second, the index is stamped .9,
    the file is edited at .5, and only then is the index rewritten.
    """
    sandbox.start({"f.txt": "VALUE = 1\n", "g.txt": "g\n"})
    repo = sandbox.repo
    git_dir = repo.bind().git_dir
    second_ns = 1_000_000_000
    base_ns = 1_000_000_000 * second_ns
    recorded = base_ns + second_ns // 10
    index_stamp = base_ns + 9 * second_ns // 10
    edited = base_ns + 5 * second_ns // 10
    f_file = sandbox.write("f.txt", "VALUE = 1\n")
    os.utime(f_file, ns=(recorded, recorded))
    repo.stage_all(["f.txt"])
    sandbox.write("g.txt", "changed\n")
    repo.stage_all(["g.txt"])
    os.utime(git_dir / "index", ns=(index_stamp, index_stamp))

    sandbox.write("f.txt", "VALUE = 9\n")
    os.utime(f_file, ns=(edited, edited))
    repo.unstage(["g.txt"])
    repo.stage_all(["."])

    assert repo.has_staged_changes(["f.txt"])
