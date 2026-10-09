"""``GitTracker.current_sha`` always equals ``git rev-parse HEAD``.

The tracker reads ``HEAD`` without spawning Git. These properties drive real
Git through tracker-owned writes, writes by other processes, ref packing,
detached heads and tracker restarts, and require the answer to match Git after
every step. A stale answer would be worse than a slow one.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support import run_test_command

from vs_project.api import GitTracker, NullGitTrackerEvents

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

_IDENTITY = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}

_OPERATIONS = st.sampled_from(
    [
        "snapshot",
        "external_commit",
        "reset_back",
        "pack_refs",
        "detach",
        "reattach",
        "update_ref",
        "restart",
    ]
)


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_test_command(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=check,
        env={**os.environ, **_IDENTITY},
    )


def _real_head(root: Path) -> str | None:
    result = _git(root, "rev-parse", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _fresh_tracker(root: Path, *, existing: bool) -> GitTracker:
    tracker = GitTracker(root, run_id="head-run", events=NullGitTrackerEvents())
    tracker.init(existing=existing)
    return tracker


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(operations=st.lists(_OPERATIONS, min_size=1, max_size=12))
def test_current_sha_matches_git_after_any_operation_sequence(
    tmp_path: Path, operations: list[str]
) -> None:
    root = tmp_path / f"repo-{len(list(tmp_path.iterdir()))}"
    root.mkdir()
    (root / "main.py").write_text("VALUE = 0\n", encoding="utf-8")
    tracker = _fresh_tracker(root, existing=False)
    assert tracker.current_sha() == _real_head(root)

    for step, operation in enumerate(operations):
        (root / "main.py").write_text(f"VALUE = {step + 1}\n", encoding="utf-8")
        if operation == "snapshot":
            tracker.snapshot(f"step {step}")
        elif operation == "external_commit":
            _git(root, "commit", "-q", "-a", "--allow-empty", "-m", f"external {step}")
        elif operation == "reset_back":
            _git(root, "reset", "-q", "--hard", "HEAD~1", check=False)
        elif operation == "pack_refs":
            _git(root, "pack-refs", "--all")
        elif operation == "detach":
            _git(root, "checkout", "-q", "--detach")
        elif operation == "reattach":
            _git(root, "checkout", "-q", "-B", "side", "HEAD")
        elif operation == "update_ref":
            _git(root, "update-ref", "HEAD", "HEAD~1", check=False)
        else:
            _git(root, "reset", "-q", "--hard")
            tracker = _fresh_tracker(root, existing=True)
        assert tracker.current_sha() == _real_head(root), operation


def test_current_sha_is_none_before_the_first_commit(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    tracker = GitTracker(tmp_path, run_id="head-run", events=NullGitTrackerEvents())
    assert tracker.current_sha() is None
    assert _real_head(tmp_path) is None


def test_current_sha_follows_a_commit_made_by_another_process(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("VALUE = 0\n", encoding="utf-8")
    tracker = _fresh_tracker(tmp_path, existing=False)
    before = tracker.current_sha()
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "by the agent")
    after = tracker.current_sha()
    assert after == _real_head(tmp_path)
    assert after != before


@settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    commits=st.lists(st.sampled_from(["commit", "pack_refs", "detach"]), min_size=1, max_size=6),
)
def test_current_sha_matches_git_in_a_linked_worktree(tmp_path: Path, commits: list[str]) -> None:
    main = tmp_path / f"main-{len(list(tmp_path.iterdir()))}"
    main.mkdir()
    (main / "main.py").write_text("VALUE = 0\n", encoding="utf-8")
    _fresh_tracker(main, existing=False)
    linked = tmp_path / f"{main.name}-linked"
    _git(main, "worktree", "add", "-q", "-b", "linked", str(linked))
    # An unbound tracker, as for a candidate worktree: it finds the repository itself.
    tracker = GitTracker(linked, run_id="head-run", events=NullGitTrackerEvents())
    assert tracker.current_sha() == _real_head(linked)

    for step, operation in enumerate(commits):
        if operation == "commit":
            _git(linked, "commit", "-q", "--allow-empty", "-m", f"linked {step}")
        elif operation == "pack_refs":
            _git(linked, "pack-refs", "--all")
        else:
            _git(linked, "checkout", "-q", "--detach")
        assert tracker.current_sha() == _real_head(linked), operation
        assert tracker.current_sha() == _real_head(linked)
