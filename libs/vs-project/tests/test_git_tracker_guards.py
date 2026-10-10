"""Guard and error-path contracts for :class:`vs_project.api.GitTracker`."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support import run_test_command

from vs_project.api import (
    CliGitRepository,
    GitCommandError,
    GitFaultSink,
    GitTracker,
    LocalAtomicWriteEffects,
    NullGitTrackerEvents,
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    atomic_write_bytes,
)
from vs_project.api.testing import run_execution_record

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

_IDENTITY = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(root: Path, *args: str) -> str:
    result = run_test_command(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_IDENTITY},
    )
    return result.stdout.strip()


def _tracker(root: Path, run_id: str = "guard-run") -> GitTracker:
    return GitTracker(root, run_id=run_id, events=NullGitTrackerEvents())


def _committed_repository(root: Path) -> str:
    _git(root, "init", "-q", "-b", "main")
    (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "main.py")
    _git(root, "commit", "-q", "-m", "baseline")
    return _git(root, "rev-parse", "HEAD")


def test_diff_patch_reads_only_the_requested_literal_paths(tmp_path: Path) -> None:
    base = _committed_repository(tmp_path)
    (tmp_path / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("OTHER = True\n", encoding="utf-8")
    _git(tmp_path, "add", "main.py", "other.py")
    _git(tmp_path, "commit", "-q", "-m", "change files")
    head = _git(tmp_path, "rev-parse", "HEAD")

    patch = _tracker(tmp_path).diff_patch(base, head, ("main.py",))

    assert patch is not None
    assert "+VALUE = 2" in patch
    assert "other.py" not in patch


@pytest.mark.parametrize("value", ["HEAD~1", "--output=escape"])
def test_diff_patch_rejects_revision_expressions(tmp_path: Path, value: str) -> None:
    _committed_repository(tmp_path)

    with pytest.raises(ValueError, match="not a commit object name"):
        _tracker(tmp_path).diff_patch(value, "a" * 40, ("main.py",))


def _initialized_tracker(root: Path, run_id: str = "guard-run") -> GitTracker:
    (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    tracker = _tracker(root, run_id)
    tracker.init(existing=False)
    return tracker


def _project(root: Path, tracker: GitTracker) -> Project:
    project = Project.open(root)
    project.state.create_project("Guard test")
    assert tracker.trusted_input_baseline is not None
    project.state.create_run(
        project.state.new_run_manifest(
            "Guard test",
            run_id=tracker.run_id,
            branch=tracker.project_branch,
            vibesys_version="test",
            trusted_input_baseline=tracker.trusted_input_baseline,
            run_environment=RunEnvironmentRecord(name="docker"),
            execution=run_execution_record(),
            orchestration=OrchestrationDescriptor(id="guard", config_version=1, options={}),
        )
    )
    tracker.snapshot_with_framework_metadata(
        "initialize state", project.state.initialization_snapshot(tracker.run_id)
    )
    return project


@pytest.mark.parametrize("bad", ["/abs/path", "", ".", "a/../b", "../up"])
def test_trusted_input_paths_must_be_normalized_relative(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match=r"project path must be a normalized relative path"):
        GitTracker(
            tmp_path,
            run_id="guard-run",
            events=NullGitTrackerEvents(),
            trusted_input_paths=[bad],
        )


def test_project_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match=r"project root must be an existing directory"):
        _tracker(missing)
    file_root = tmp_path / "file"
    file_root.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match=r"project root must be an existing directory"):
        _tracker(file_root)


@pytest.mark.parametrize("candidate_id", ["m-a..b-0123", "a.lock", "a.", "a..b"])
def test_retain_candidate_names_ids_that_git_rejects_as_ref_components(
    tmp_path: Path, candidate_id: str
) -> None:
    tracker = _initialized_tracker(tmp_path)
    with pytest.raises(ValueError, match="not a valid Git ref name component"):
        tracker.retain_candidate(candidate_id, "HEAD")
    assert tracker.retain_candidate("m-a.b-0123", "HEAD").endswith("/candidates/m-a.b-0123")


def test_retain_worktree_reports_git_failure_in_candidate_worktree(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    worktree = Project.open(tmp_path).state.candidate_worktree_directory("guard-run", "cand-1")
    worktree.mkdir(parents=True)
    _git(worktree, "init", "-q")
    with pytest.raises(RuntimeError, match=r"Git command failed in candidate worktree"):
        tracker.retain_worktree(worktree, "cand-1")


class _RepositoryWithoutRoots(CliGitRepository):
    """A repository whose history reports no parentless commit."""

    def root_commit(self, revision: str) -> str | None:
        del revision
        return None


def test_candidate_patch_requires_a_resolvable_baseline(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    events = NullGitTrackerEvents()
    tracker = GitTracker(
        tmp_path,
        run_id="guard-run",
        events=events,
        repository=_RepositoryWithoutRoots(tmp_path, faults=events),
    )
    tracker.init(existing=False)
    sha = tracker.current_sha()
    assert sha is not None
    with pytest.raises(
        ValueError, match=re.escape(f"cannot resolve workspace baseline for commit {sha}")
    ):
        tracker.candidate_patch(sha)


def test_framework_namespace_must_be_a_directory(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    project = _project(tmp_path, tracker)
    namespace = project.state.portable_namespace("guard-run", "evolve")
    (namespace.external_directory() / "state.json").write_text("{}\n", encoding="utf-8")
    snapshot = namespace.snapshot()
    root = namespace.external_directory()
    (root / "state.json").unlink()
    root.rmdir()
    root.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"framework state namespace is not a directory"):
        tracker.snapshot_framework_state("replace", snapshot)
    assert root.read_text(encoding="utf-8") == "not a directory\n"


class _RecordingEvents(NullGitTrackerEvents):
    def __init__(self) -> None:
        self.warnings: list[tuple[str, str | None]] = []

    def warning(self, summary: str, *, detail: str | None = None) -> None:
        self.warnings.append((summary, detail))


class _RepositoryWhoseRestoreReplacesMemory(CliGitRepository):
    """A repository whose worktree restore fails after replacing ``memory`` with a file."""

    def __init__(self, root: Path, *, faults: GitFaultSink) -> None:
        super().__init__(root, faults=faults)
        self._memory = root / "memory"

    def restore_worktree(self, revision: str, exclude: Sequence[str] = ()) -> None:
        # The rollback replaces the preserved directory with a file, so both
        # the restore and the later re-application of memory fail.
        del exclude
        (self._memory / "notes.txt").unlink()
        self._memory.rmdir()
        self._memory.write_text("blocker\n", encoding="utf-8")
        raise GitCommandError(["git", "restore", revision], 1, "restore failed")


def test_checkout_tree_reports_failed_restore_of_preserved_memory(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    events = _RecordingEvents()
    tracker = GitTracker(
        tmp_path,
        run_id="guard-run",
        events=events,
        repository=_RepositoryWhoseRestoreReplacesMemory(tmp_path, faults=events),
    )
    tracker.init(existing=False)
    sha = tracker.current_sha()
    assert sha is not None
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "notes.txt").write_text("keep\n", encoding="utf-8")

    assert tracker.checkout_tree(sha, preserve_paths=["memory"]) is False
    messages = [message for message, _detail in events.warnings]
    assert messages == [
        "failed to restore preserved workspace memory after tree restore error",
        f"git tree restore {sha[:8]} failed",
    ]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a read-only file")
def test_checkout_tree_keeps_preserved_memory_the_user_cannot_write(tmp_path: Path) -> None:
    """A preserved file the user may not write must not fail the restore.

    An agent that writes evidence from a root container leaves a root-owned,
    mode 0644 file in the workspace. Mode 0444 reproduces that for a non-root
    user: the directory still allows replacing the file, as ``git restore``
    does, but opening it for writing fails.
    """
    tracker = _initialized_tracker(tmp_path)
    memory = tmp_path / "memory"
    memory.mkdir()
    evidence = memory / "heap.json"
    evidence.write_text("{}\n", encoding="utf-8")
    tracker.snapshot("candidate with evidence")
    winner = tracker.current_sha()
    assert winner is not None
    evidence.chmod(0o444)

    assert tracker.checkout_tree(winner, clean=True, preserve_paths=["memory"]) is True
    assert evidence.read_text(encoding="utf-8") == "{}\n"


def test_checkout_tree_keeps_index_clean_when_restoring_an_earlier_revision(
    tmp_path: Path,
) -> None:
    tracker = _initialized_tracker(tmp_path)
    (tmp_path / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    tracker.snapshot("candidate one")
    first_candidate = tracker.current_sha()
    assert first_candidate is not None

    (tmp_path / "main.py").write_text("VALUE = 3\n", encoding="utf-8")
    (tmp_path / "later.py").write_text("LATER = True\n", encoding="utf-8")
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "notes.txt").write_text("keep across rollback\n", encoding="utf-8")
    tracker.snapshot("candidate two")
    latest_head = tracker.current_sha()
    assert latest_head is not None
    assert latest_head != first_candidate

    assert tracker.checkout_tree(first_candidate, clean=True, preserve_paths=["memory"])

    assert tracker.current_sha() == latest_head
    assert (tmp_path / "main.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert not (tmp_path / "later.py").exists()
    assert (memory / "notes.txt").read_text(encoding="utf-8") == "keep across rollback\n"
    assert not tracker.has_staged_changes()


@pytest.mark.parametrize("bad", ["/abs/memory", "", "a/../../etc"])
def test_checkout_tree_rejects_unsafe_preserve_paths(tmp_path: Path, bad: str) -> None:
    tracker = _initialized_tracker(tmp_path)
    sha = tracker.current_sha()
    assert sha is not None
    with pytest.raises(ValueError, match=r"preserved path must be workspace-relative"):
        tracker.checkout_tree(sha, preserve_paths=[bad])


def test_trusted_input_baseline_must_be_an_existing_commit(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    with pytest.raises(ValueError, match=r"trusted input baseline 'deadbeef' is not a commit"):
        tracker.configure_trusted_input_baseline("deadbeef")
    assert tracker.trusted_input_baseline != "deadbeef"


def test_trusted_input_baseline_must_be_an_ancestor_of_head(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    _git(tmp_path, "switch", "-q", "-c", "side", "main")
    (tmp_path / "side.py").write_text("SIDE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "side.py")
    _git(tmp_path, "commit", "-q", "-m", "side")
    side_sha = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "switch", "-q", tracker.project_branch)
    before = tracker.trusted_input_baseline

    with pytest.raises(ValueError, match=r"is not an ancestor of HEAD"):
        tracker.configure_trusted_input_baseline(side_sha)
    assert tracker.trusted_input_baseline == before


@pytest.mark.parametrize("run_id", ["bad..id", "trailing.lock", "ends-with-dot."])
def test_run_id_must_form_a_valid_git_branch(tmp_path: Path, run_id: str) -> None:
    tracker = _tracker(tmp_path, run_id)
    with pytest.raises(ValueError, match=r"invalid VibeSys run id for a Git branch"):
        tracker.init(existing=False)
    assert not (tmp_path / ".git").exists()


def test_trusted_input_baseline_is_only_valid_when_resuming(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    tracker = _tracker(tmp_path)
    with pytest.raises(ValueError, match=r"only valid when resuming a run"):
        tracker.init(existing=False, trusted_input_baseline="deadbeef")


def test_start_fails_when_baseline_commit_cannot_be_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    tracker = _tracker(tmp_path)
    monkeypatch.setattr(tracker, "current_sha", lambda: None)
    with pytest.raises(ValueError, match=r"user-project baseline commit could not be resolved"):
        tracker.init(existing=False)
    assert tracker.trusted_input_baseline is None


def test_start_refuses_existing_run_branch(tmp_path: Path) -> None:
    _committed_repository(tmp_path)
    tracker = _tracker(tmp_path)
    _git(tmp_path, "branch", tracker.project_branch)
    with pytest.raises(
        ValueError, match=re.escape(f"VibeSys run branch already exists: {tracker.project_branch}")
    ):
        tracker.init(existing=False)
    assert _git(tmp_path, "branch", "--show-current") == "main"


def test_init_fails_when_project_history_cannot_be_inspected(tmp_path: Path) -> None:
    _committed_repository(tmp_path)
    blob = _git(tmp_path, "rev-parse", "HEAD:main.py")
    (tmp_path / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    tracker = _tracker(tmp_path)
    with pytest.raises(ValueError, match=r"cannot inspect project Git history for private inputs"):
        tracker.init(existing=False)


def test_reading_pending_changes_never_writes_the_repository_index(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    target = tmp_path / "main.py"
    # Rewrite identical content with a new mtime: the index entry is now
    # stat-stale, which is what makes a plain `git status` write a refreshed
    # index back under .git/index.lock.
    stat = target.stat()
    target.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    index = tmp_path / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    assert tracker.pending_changes() == []

    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


_NAMESPACE = "turn-state"
_FRAMEWORK_FILES = ("a.json", "b.json")
_AGENT_PATHS = (
    "main.py",
    "pkg/new.py",
    ".vibesys/other.txt",
    ".vibesys/state/runs/guard-run/turn-state/a.json",
    ".vibesys/state/runs/guard-run/turn-state/b.json",
    ".vibesys/state/runs/guard-run/turn-state/stray.txt",
)
_BASELINE = {
    "main.py": b"VALUE = 1\n",
    ".vibesys/state/runs/guard-run/turn-state/a.json": b"fw-initial",
}


@st.composite
def _turn(draw: st.DrawFn) -> list[tuple[str, str, bytes | None]]:
    """Interleaved framework and agent writes; agent bytes never equal framework bytes."""
    framework = st.tuples(
        st.just("framework"),
        st.sampled_from(_FRAMEWORK_FILES),
        st.sampled_from([b"fw-0", b"fw-1", b"fw-initial"]),
    )
    agent = st.tuples(
        st.just("agent"),
        st.sampled_from(_AGENT_PATHS),
        st.sampled_from([None, b"agent-0", b"agent-1", b"VALUE = 1\n"]),
    )
    return draw(st.lists(st.one_of(framework, agent), max_size=8))


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(turn=_turn())
def test_isolation_reports_exactly_what_the_agent_wrote_including_below_vibesys(
    tmp_path_factory: pytest.TempPathFactory, turn: list[tuple[str, str, bytes | None]]
) -> None:
    """Framework state written during a turn is not the agent's; any other write is.

    The framework publishes run state below ``.vibesys`` while a turn is in flight, so
    the isolation check cannot ignore that directory without letting a read-only role
    write there unnoticed. Whatever the interleaving, the paths reported are exactly
    those whose last writer was the agent and whose content then differs from the
    baseline the turn started from.
    """
    root = tmp_path_factory.mktemp("project")
    tracker = _initialized_tracker(root)
    project = _project(root, tracker)
    namespace = project.state.portable_namespace(tracker.run_id, _NAMESPACE)
    namespace.write_bytes("a.json", _BASELINE[".vibesys/state/runs/guard-run/turn-state/a.json"])
    tracker.snapshot_framework_state("baseline framework state", namespace.snapshot())
    assert tracker.pending_changes() == []

    final: dict[str, bytes | None] = {}
    last_writer: dict[str, str] = {}
    for writer, name, content in turn:
        if writer == "framework":
            assert content is not None
            namespace.write_bytes(name, content)
            path = namespace.agent_visible_path(name)
        else:
            path = name
            target = root / path
            if content is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        final[path] = content
        last_writer[path] = writer

    expected = sorted(
        path
        for path, writer in last_writer.items()
        if writer == "agent" and final[path] != _BASELINE.get(path)
    )
    assert tracker.pending_changes() == expected


def test_an_agent_overwrite_of_a_framework_file_is_reported(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    project = _project(tmp_path, tracker)
    namespace = project.state.portable_namespace(tracker.run_id, _NAMESPACE)
    namespace.write_bytes("a.json", b"fw-0")
    assert tracker.pending_changes() == []

    (tmp_path / namespace.agent_visible_path("a.json")).write_bytes(b"agent-0")

    assert tracker.pending_changes() == [namespace.agent_visible_path("a.json")]


class _CheckedMidPublication(LocalAtomicWriteEffects):
    """Local effects that run ``during`` right after the file is replaced, before publication ends.

    A read-only turn is checked on another thread while the framework commits run state,
    so the check can land at any point of a commit. This lands it at the point where the
    new bytes are on disk and the commit has not finished, deterministically.
    """

    def __init__(self, during: Callable[[], None]) -> None:
        self._during = during

    def replace(self, temporary: Path, destination: Path) -> None:
        super().replace(temporary, destination)
        self._during()


@pytest.mark.parametrize("overwrite", [False, True], ids=["first-write", "overwrite"])
def test_an_isolation_check_during_a_framework_commit_does_not_blame_the_agent(
    tmp_path: Path, *, overwrite: bool
) -> None:
    """Regression: a planner turn failed with 'wrote outside its workspace access' on store.json.

    The check landed between the framework replacing its state file and recording what it
    wrote, saw bytes it had no record of, and attributed them to the read-only agent.
    """
    tracker = _initialized_tracker(tmp_path)
    state_file = tmp_path / ".vibesys" / "state" / "runs" / "guard-run" / "turn-state" / "a.json"
    if overwrite:
        atomic_write_bytes(state_file, b"fw-0")
    else:
        state_file.parent.mkdir(parents=True)
    seen: list[list[str]] = []
    effects = _CheckedMidPublication(lambda: seen.append(tracker.pending_changes()))

    atomic_write_bytes(state_file, b"fw-1", effects=effects)

    assert seen == [[]]
    assert tracker.pending_changes() == []
    state_file.write_bytes(b"agent")
    assert tracker.pending_changes() == [state_file.relative_to(tmp_path).as_posix()]


def test_files_a_library_writes_in_a_framework_directory_are_not_the_agents(
    tmp_path: Path,
) -> None:
    """A path-based library (the artifact store) writes inside the directory it is handed."""
    tracker = _initialized_tracker(tmp_path)
    project = _project(tmp_path, tracker)
    namespace = project.state.portable_namespace(tracker.run_id, _NAMESPACE)
    (namespace.external_directory("objects") / "0123abcd").write_bytes(b"object")
    assert tracker.pending_changes() == []

    stray = tmp_path / ".vibesys" / "stray.txt"
    stray.write_bytes(b"agent")

    assert tracker.pending_changes() == [".vibesys/stray.txt"]


def test_is_retained_separates_reachable_commits_from_dangling_and_unknown_ones(
    tmp_path: Path,
) -> None:
    tracker = _initialized_tracker(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD")
    tree = _git(tmp_path, "rev-parse", "HEAD^{tree}")
    dangling = _git(tmp_path, "commit-tree", tree, "-m", "dangling")
    held = _git(tmp_path, "commit-tree", tree, "-p", head, "-m", "held")
    child = _git(tmp_path, "commit-tree", tree, "-p", held, "-m", "child")
    assert tracker.is_retained(head)
    assert not tracker.is_retained(dangling)
    assert not tracker.is_retained(held)
    tracker.retain_candidate("held-one", child)
    assert tracker.is_retained(child)
    assert tracker.is_retained(held)  # an ancestor of a retained tip is retained
    assert not tracker.is_retained(dangling)
    assert not tracker.is_retained("0" * 40)
    with pytest.raises(ValueError, match="not a commit object name"):
        tracker.is_retained("--all")
