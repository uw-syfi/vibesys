"""A state-store write in flight never fails the tracker's whole-tree Git scans.

``git add`` lists a directory and then stats each entry, so a file that is
renamed away in between aborts it with "unable to stat". The framework's state
store does exactly that to its ``.store.json.*.tmp`` files under
``.vibesys/state/runs/<run>/core-store``. A timing-based test cannot force that
window, so these tests make it deterministic: ``_ScanRepository`` asks real Git
which untracked paths a request selects, lets a store write finish (the temp
file is renamed to its destination), and only then stats what was listed, as
Git does. A scan that selects the state directory fails every time; a scan that
excludes it by path cannot.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support import run_test_command

from vs_project.api import (
    CliGitRepository,
    GitFaultSink,
    GitTracker,
    NullGitTrackerEvents,
    StagingError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_NAMES = st.text(alphabet="abcdefgh01", min_size=1, max_size=6)


class _StoreWriter:
    """A state-store writer that is mid-write whenever a scan begins."""

    def __init__(self, root: Path, run_id: str) -> None:
        self._store = root / ".vibesys" / "state" / "runs" / run_id / "core-store"
        self._store.mkdir(parents=True)
        self._temps: list[Path] = []
        self.writes = 0

    def begin(self, count: int) -> None:
        """Create ``count`` temp files, as the store does before publishing."""
        for index in range(count):
            temp = self._store / f".store.json.{self.writes}-{index}.tmp"
            temp.write_text("partial\n")
            self._temps.append(temp)

    def finish(self) -> None:
        """Publish: rename every temp over the destination, so the temp is gone."""
        for temp in self._temps:
            temp.replace(self._store / "store.json")
        self.writes += 1
        self._temps.clear()


class _ScanRepository(CliGitRepository):
    """Real Git, but every whole-tree scan has a store write finish between list and stat."""

    def __init__(
        self, root: Path, *, faults: GitFaultSink, writer: _StoreWriter, temps: int
    ) -> None:
        super().__init__(root, faults=faults)
        self._root = root
        self._writer = writer
        self.temps = temps

    def _vanished(self, pathspecs: Sequence[str]) -> str | None:
        self._writer.begin(self.temps)
        listing = run_test_command(
            ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", *pathspecs],
            cwd=self._root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        listed = [path for path in listing.split("\0") if path]
        self._writer.finish()
        return next((path for path in listed if not os.path.lexists(self._root / path)), None)

    def stage_all(self, pathspecs: Sequence[str], *, force: bool = False) -> None:
        gone = self._vanished(pathspecs)
        if gone is not None:
            message = f"fatal: unable to stat '{gone}': No such file or directory"
            raise StagingError(["git", "add", "-A", "--", *pathspecs], 128, message, ())
        super().stage_all(pathspecs, force=force)

    def worktree_matches(
        self, revision: str, pathspecs: Sequence[str], *, include_ignored: bool
    ) -> bool:
        if self._vanished(pathspecs) is not None:
            return False  # the CLI implementation reports a failed scratch add as "no match"
        return super().worktree_matches(revision, pathspecs, include_ignored=include_ignored)


def _tracker(root: Path, run_id: str, temps: int) -> tuple[GitTracker, _ScanRepository]:
    (root / "a.txt").write_text("1\n")
    events = NullGitTrackerEvents()
    writer = _StoreWriter(root, run_id)
    repository = _ScanRepository(root, faults=events, writer=writer, temps=0)
    tracker = GitTracker(root, run_id="run", events=events, repository=repository)
    tracker.init(existing=False)
    repository.temps = temps  # writers start only after init
    return tracker, repository


def _committed_paths(root: Path) -> list[str]:
    return run_test_command(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()


def test_the_harness_forces_the_failure_for_a_scan_that_selects_the_state_directory() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch).resolve()
        _, repository = _tracker(root, "r1", temps=1)

        with pytest.raises(StagingError, match="unable to stat"):
            repository.stage_all(["."])


@settings(max_examples=15, deadline=None)
@given(run_id=_NAMES, temps=st.integers(min_value=1, max_value=4))
def test_snapshot_survives_a_store_write_in_flight_at_every_scan(run_id: str, temps: int) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch).resolve()
        tracker, _ = _tracker(root, run_id, temps)
        (root / "a.txt").write_text("2\n")

        tracker.snapshot("edit")

        assert (
            run_test_command(
                ["git", "show", "HEAD:a.txt"], cwd=root, capture_output=True, text=True, check=True
            ).stdout
            == "2\n"
        )
        assert not [path for path in _committed_paths(root) if path.startswith(".vibesys/state")]


@settings(max_examples=15, deadline=None)
@given(run_id=_NAMES, temps=st.integers(min_value=1, max_value=4))
def test_tree_comparison_survives_a_store_write_in_flight(run_id: str, temps: int) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch).resolve()
        tracker, repository = _tracker(root, run_id, temps=0)
        tracker.snapshot("edit")
        head = tracker.current_sha()
        assert head is not None
        repository.temps = temps

        assert tracker.matches_tree(head)
