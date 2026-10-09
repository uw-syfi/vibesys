"""``GitTracker`` never lets Git scan framework state while staging candidate edits.

The framework's state store creates and renames temporary files under
``.vibesys/state`` from other threads while a snapshot runs. ``git add -A``
that scans such a file fails with "unable to stat" when it vanishes between the
directory read and the stat. Retrying hides that; not scanning removes it. The
observable rule: the pathspecs a snapshot stages select nothing in the state
subtree, whatever is stored there, and candidate files are still committed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support import run_test_command

from vs_project.api import (
    CliGitRepository,
    GitFaultSink,
    GitTracker,
    NullGitTrackerEvents,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_STATE = ".vibesys/state"
_NAMES = st.text(alphabet="abcdefgh01_-", min_size=1, max_size=8)


class _RecordingRepository(CliGitRepository):
    """The real CLI repository, remembering every ``stage_all`` request."""

    def __init__(self, root: Path, *, faults: GitFaultSink) -> None:
        super().__init__(root, faults=faults)
        self.staged: list[tuple[str, ...]] = []

    def stage_all(self, pathspecs: Sequence[str], *, force: bool = False) -> None:
        self.staged.append(tuple(pathspecs))
        super().stage_all(pathspecs, force=force)


def _selected_untracked(root: Path, pathspecs: Sequence[str]) -> list[str]:
    """Untracked files that Git itself says ``pathspecs`` select."""
    result = run_test_command(
        ["git", "ls-files", "--others", "--", *pathspecs],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


@settings(max_examples=20, deadline=None)
@given(
    run_id=_NAMES,
    store_files=st.lists(_NAMES, min_size=1, max_size=4, unique=True),
)
def test_snapshot_does_not_stage_framework_state(run_id: str, store_files: list[str]) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch).resolve()
        (root / "a.txt").write_text("1\n")
        events = NullGitTrackerEvents()
        repository = _RecordingRepository(root, faults=events)
        tracker = GitTracker(root, run_id="run", events=events, repository=repository)
        tracker.init(existing=False)
        store = root / _STATE / "runs" / run_id / "core-store"
        store.mkdir(parents=True)
        for name in store_files:
            (store / f".{name}.tmp").write_text("temp\n")
        (root / "a.txt").write_text("2\n")
        repository.staged.clear()

        tracker.snapshot("edit")

        assert repository.staged
        for pathspecs in repository.staged:
            leaked = [
                path
                for path in _selected_untracked(root, pathspecs)
                if path.startswith(f"{_STATE}/")
            ]
            assert not leaked
        assert repository.read_blob("HEAD", "a.txt") == b"2\n"
