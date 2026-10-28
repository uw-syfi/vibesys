"""What a settled attempt changed, read from the run's repository and kept on its workstream span."""

from __future__ import annotations

import json
from functools import partial
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_proto.api as p
from vibesys.events import CoreEventType, SpanEndedData, SpanStartedData
from vibesys.run import EventJournal
from vibesys.workspace_changes import DIFF_LIMIT, capped_diff, read_attempt_changes
from vs_project.api import GitTracker, NullGitTrackerEvents, Project
from vs_project.api.testing import FakeGitRepositories
from vs_runlog.api import RunLogReader
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import GitRepository

_MEMORY = "memory"


class _Repository:
    """A project repository in memory over a real working tree, committed as a run commits."""

    def __init__(self, root: Path) -> None:
        root.mkdir()
        self.root = root
        self.git: GitRepository = FakeGitRepositories().repository(root)
        self.git.initialize(initial_branch="main")
        self.git.bind()
        self.tracker = GitTracker(
            root, run_id="run-1", events=NullGitTrackerEvents(), repository=self.git
        )

    def commit(self, files: dict[str, str | None]) -> str:
        for path, text in files.items():
            target = self.root / path
            if text is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
        self.git.stage_all(["."])
        self.git.commit("attempt")
        head = self.git.head()
        assert head is not None
        return head


def _workstream(base: str, head: str, *, ended: bool) -> SpanStartedData:
    return SpanStartedData.model_validate(
        {
            "span": {
                "id": "op:work",
                "parent": None,
                "type": "operation",
                "name": "attempt",
                "status": "done" if ended else "running",
                "started_at": 0,
                "ended_at": 3 if ended else None,
                "label": {
                    "kind": "workstream",
                    "attrs_json": json.dumps(
                        {"parent_revision": base, **({"revision": head} if ended else {})}
                    ),
                },
            }
        }
    )


def test_a_settled_attempt_keeps_the_users_files_and_their_diff_in_the_run_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "home"))
    repository = _Repository(tmp_path / "project")
    base = repository.commit({"service.py": "VALUE = 1\n", "old.py": "gone\n"})
    head = repository.commit(
        {
            "service.py": "VALUE = 2\n",
            "old.py": None,
            "new.py": "added\n",
            f"{_MEMORY}/notes.md": "framework notes\n",
        }
    )
    project = Project.open(repository.root)
    journal = EventJournal(ManualClock(), SimThreads())
    journal.enable_runlog(project, "run-1")
    journal.attach(Project.log_directory_for(project.root, "run-1"), "run-1")
    journal.follow_attempt_changes(
        partial(read_attempt_changes, repository.tracker, framework=(_MEMORY,))
    )

    journal.emit(CoreEventType.SPAN_STARTED, "", data=_workstream(base, head, ended=False))
    journal.emit(
        CoreEventType.SPAN_ENDED,
        "",
        data=SpanEndedData(span=_workstream(base, head, ended=True).span),
    )
    journal.close()

    with Project.run_log_path_for(project.root, "run-1").open("rb") as stream:
        records = list(RunLogReader(stream))
    spans = [
        record.body.value.span
        for record in records
        if record.body is not None and record.body.field == "span" and record.body.value.span
    ]
    last = spans[-1]
    assert last.label is not None
    assert last.label.attrs is not None
    files = p.WorkstreamAttrs.from_binary(last.label.attrs.value).files
    assert sorted(file.path for file in files) == ["new.py", "old.py", "service.py"]
    attached = [
        record.body.value
        for record in records
        if record.body is not None and record.body.field == "blob"
    ]
    assert [(each.span, each.role) for each in attached] == [("op:work", "diff")]
    blob = attached[0].blob
    assert blob is not None
    diff = (Project.run_blobs_directory_for(project.root, "run-1") / blob.sha256).read_bytes()
    for file in files:
        part = diff[file.diff_offset : file.diff_offset + file.diff_length]
        assert part.startswith(f"diff --git a/{file.path} b/{file.path}\n".encode())
    assert _MEMORY.encode() not in diff


@pytest.mark.parametrize(("base", "head"), [("HEAD~1", "HEAD"), ("--output=x", "0" * 40)])
def test_a_range_that_names_no_plain_commits_reads_as_unreadable(
    tmp_path: Path, base: str, head: str
) -> None:
    repository = _Repository(tmp_path / "project")
    repository.commit({"service.py": "VALUE = 1\n"})
    assert read_attempt_changes(repository.tracker, base, head) is None


@given(lines=st.lists(st.integers(min_value=0, max_value=DIFF_LIMIT // 3), max_size=6))
def test_a_capped_diff_keeps_whole_lines_and_says_when_it_was_cut(lines: list[int]) -> None:
    diff = "".join("x" * length + "\n" for length in lines)
    capped = capped_diff(diff)
    whole = len(diff.encode())
    if whole <= DIFF_LIMIT:
        assert capped == diff
        return
    kept, marker = capped.rsplit("\n", 2)[0] + "\n", capped.splitlines()[-1]
    assert diff.startswith(kept)
    assert len(kept.encode()) <= DIFF_LIMIT
    assert marker == f"[diff truncated: {len(kept.encode())} of {whole} bytes shown]"
