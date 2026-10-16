"""``GitTracker.measured_input`` names what an evaluation of a commit reads (#2949).

Two commits have one measured input exactly when they agree on every path the evaluation
reads: a change confined to the paths the task declares unread (an agent's notes) keeps the
digest, any other change moves it. The property drives real Git on every Git backend.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from tests.support.real_git import real_git_tracker

from vs_project.api import GIT_BACKEND_ENV, NullGitTrackerEvents
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from pathlib import Path

_READ = ("main.py", "src/kernel.py")
_UNREAD = ("NOTES.md", "notes/round.md")
_DECLARED = ("NOTES.md", "notes/")
"""The task's declaration: a file and a directory (with the trailing slash a task may write)."""


@pytest.fixture(autouse=True, params=["cli", "pygit2"])
def _backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GIT_BACKEND_ENV, request.param)


@settings(
    max_examples=examples(pr=4, nightly=25),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    edits=st.lists(
        st.tuples(st.sampled_from(_READ + _UNREAD), st.integers(min_value=0, max_value=2)),
        min_size=1,
        max_size=6,
    )
)
@example(
    edits=[
        ("NOTES.md", 1),
        ("notes/round.md", 1),
        ("main.py", 1),
        ("notes/round.md", 2),
        ("NOTES.md", 2),
        ("src/kernel.py", 1),
    ]
)
def test_commits_share_a_measured_input_iff_they_agree_on_every_read_path(
    tmp_path: Path, edits: list[tuple[str, int]]
) -> None:
    root = tmp_path / f"repo-{len(list(tmp_path.iterdir()))}"
    root.mkdir()
    (root / "main.py").write_text("VALUE = 0\n", encoding="utf-8")
    tracker = real_git_tracker(root, run_id="input-run", events=NullGitTrackerEvents())
    tracker.init(existing=False)
    contents: dict[str, int] = {}
    seen: list[tuple[str, tuple[tuple[str, int], ...]]] = []
    for number, (path, value) in enumerate(edits):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{path} = {value}\n", encoding="utf-8")
        contents[path] = value
        tracker.snapshot(f"step-{number}")
        commit = tracker.current_sha()
        assert commit is not None
        read = tuple(sorted((key, item) for key, item in contents.items() if key in _READ))
        seen.append((tracker.measured_input(commit, _DECLARED), read))
    for digest, read in seen:
        for other_digest, other_read in seen:
            assert (digest == other_digest) == (read == other_read)
