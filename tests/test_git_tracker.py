"""GitTracker unit tests against real temporary git repos (no mocks)."""

from __future__ import annotations

import subprocess

import pytest

from vibesys.run import GitTracker

_EXCLUDED = {".git", "__pycache__", "target"}


def _make_tracker(ws, logs=None):
    log = logs.append if logs is not None else (lambda _msg: None)
    return GitTracker(ws, log=log, excluded_dirs=_EXCLUDED)


def _git_stdout(ws, *args) -> str:
    return subprocess.run(["git", *args], cwd=ws, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def ws(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "main.py").write_text("VALUE = 1\n")
    return ws


def test_init_creates_repo_gitignore_and_initial_commit(ws):
    tracker = _make_tracker(ws)
    tracker.init(existing=False)

    assert (ws / ".git").is_dir()
    gitignore = (ws / ".gitignore").read_text()
    assert "target" in gitignore
    assert "*.neff" in gitignore
    log = _git_stdout(ws, "log", "--format=%s")
    assert log.strip() == "initial: workspace setup"


def test_init_appends_to_existing_gitignore(ws):
    (ws / ".gitignore").write_text("custom-entry")  # no trailing newline
    tracker = _make_tracker(ws)
    tracker.init(existing=False)

    gitignore = (ws / ".gitignore").read_text()
    assert gitignore.startswith("custom-entry\n")
    assert "*.neff" in gitignore


def test_init_uses_containing_experiment_repo_without_nesting(tmp_path):
    experiment = tmp_path / "experiment"
    workspace = experiment / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "main.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=experiment, check=True)

    tracker = _make_tracker(workspace)
    tracker.init(existing=False)

    assert not (workspace / ".git").exists()
    assert _git_stdout(workspace, "show", "--format=", "--name-only").strip().splitlines() == [
        "workspace/.gitignore",
        "workspace/main.py",
    ]


def test_init_existing_requires_repo(ws):
    tracker = _make_tracker(ws)
    with pytest.raises(ValueError, match="no git repository"):
        tracker.init(existing=True)

    tracker.init(existing=False)
    tracker.init(existing=True)  # now valid; must not re-init or commit
    log = _git_stdout(ws, "log", "--format=%s")
    assert log.strip() == "initial: workspace setup"


def test_snapshot_commits_changes_and_skips_clean_tree(ws):
    logs: list[str] = []
    tracker = _make_tracker(ws, logs)
    tracker.init(existing=False)

    (ws / "main.py").write_text("VALUE = 2\n")
    tracker.snapshot("round 1")
    assert _git_stdout(ws, "log", "-1", "--format=%s").strip() == "round 1"

    tracker.snapshot("round 2")  # nothing changed — no commit, only a log line
    assert _git_stdout(ws, "log", "-1", "--format=%s").strip() == "round 1"
    assert any("no changes to commit for 'round 2'" in line for line in logs)


def test_current_sha_matches_head_and_is_none_without_repo(ws):
    tracker = _make_tracker(ws)
    assert tracker.current_sha() is None  # no repo yet

    tracker.init(existing=False)
    assert tracker.current_sha() == _git_stdout(ws, "rev-parse", "HEAD").strip()


def test_checkout_tree_restores_snapshot_without_moving_head(ws):
    tracker = _make_tracker(ws)
    tracker.init(existing=False)
    first = tracker.current_sha()

    (ws / "main.py").write_text("VALUE = 2\n")
    tracker.snapshot("round 1")
    second = tracker.current_sha()

    assert tracker.checkout_tree(first) is True
    assert (ws / "main.py").read_text() == "VALUE = 1\n"
    # HEAD stays put so the next commit lands as a new child commit.
    assert tracker.current_sha() == second


def test_checkout_tree_clean_removes_untracked_files(ws):
    tracker = _make_tracker(ws)
    tracker.init(existing=False)
    first = tracker.current_sha()

    (ws / "leftover.txt").write_text("scratch\n")
    assert tracker.checkout_tree(first, clean=True) is True
    assert not (ws / "leftover.txt").exists()


def test_checkout_tree_returns_false_and_logs_on_bad_sha(ws):
    logs: list[str] = []
    tracker = _make_tracker(ws, logs)
    tracker.init(existing=False)

    assert tracker.checkout_tree("0000000000000000000000000000000000000000") is False
    assert any("git checkout 00000000 failed" in line for line in logs)


def test_trusted_input_changes_reports_committed_and_pending_edits(ws):
    (ws / "accuracy_checker").mkdir()
    (ws / "accuracy_checker" / "checker.py").write_text("print('ok')\n")
    tracker = _make_tracker(ws)
    tracker.init(existing=False)

    # Non-evaluator edits are not reported, committed or not.
    (ws / "main.py").write_text("VALUE = 2\n")
    tracker.snapshot("round 1")
    assert tracker.trusted_input_changes() == []

    # Pending (uncommitted) evaluator edits are reported...
    (ws / "accuracy_checker" / "checker.py").write_text("print('forged')\n")
    assert tracker.trusted_input_changes() == ["accuracy_checker/checker.py"]

    # ...and stay reported once committed (diff against the root commit).
    tracker.snapshot("round 2")
    assert tracker.trusted_input_changes() == ["accuracy_checker/checker.py"]


def test_run_is_a_public_escape_hatch(ws):
    tracker = _make_tracker(ws)
    tracker.init(existing=False)

    result = tracker.run(["git", "status", "--porcelain"], check=True)
    assert result.returncode == 0


def test_add_worktree_materializes_commit_and_shares_object_store(ws, tmp_path):
    tracker = _make_tracker(ws)
    tracker.init(existing=False)
    base_sha = tracker.current_sha()
    assert base_sha is not None

    # Advance the main tree so the worktree must reflect the OLD commit, not HEAD.
    (ws / "main.py").write_text("VALUE = 99\n")
    tracker.snapshot("advance")

    wt = tmp_path / "candidates" / "g1c1"
    tracker.add_worktree(wt, base_sha)

    # Worktree holds the parent commit's content, isolated from the main tree.
    assert (wt / "main.py").read_text() == "VALUE = 1\n"
    assert (ws / "main.py").read_text() == "VALUE = 99\n"

    # A commit in the worktree lands in the SHARED object store: the main repo
    # can resolve it by sha.
    wt_tracker = _make_tracker(wt)
    (wt / "main.py").write_text("VALUE = 7\n")
    wt_tracker.snapshot("child")
    child_sha = wt_tracker.current_sha()
    assert child_sha is not None and child_sha != base_sha
    # `git cat-file -e <sha>` in the MAIN repo succeeds → object is shared.
    assert tracker.run(["git", "cat-file", "-e", child_sha], check=False).returncode == 0

    tracker.remove_worktree(wt)
    assert not wt.exists()
    # The child commit object survives worktree removal (shared store).
    assert tracker.run(["git", "cat-file", "-e", child_sha], check=False).returncode == 0
