"""Server projection tests for the per-round design log."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack

import pytest
from tests.server.support import build_server_parts
from tests.support import run_test_command

import server.api.service as service_module
from server.api.design import _PATCH_CHAR_LIMIT, DesignLog
from server.api.protocol import DesignPatchQuery, DesignQuery
from server.api.workspace_git import WorkspacePatchReader
from vibesys.api._readmodel import project_run_view
from vibesys.api.contracts import RunStatus
from vibesys.loops.agent.model import AgentRunState, Hypothesis
from vibesys.loops.agent.state import AgentRunStateStore
from vibesys.run.git_events import NullGitTrackerEvents
from vibesys.run.git_tracker import GitTracker
from vibesys.schemas import OrchestratorPlan
from vs_loop_state.api import RoundRecord
from vs_project.api import AgentRunConfiguration, Project, RunEnvironmentRecord

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api import RunView
    from vs_project.api import GitTrackerEvents


class _RoundFields(TypedDict, total=False):
    """Keyword fields of ``RoundRecord``, so helper overrides stay checked."""

    round_number: int
    commit: str | None
    perf_metric: float | None
    perf_unit: str | None
    passed: bool
    hypothesis_id: str | None
    judge_verdict: Literal["pass", "fail", "deferred"] | None
    hypothesis_outcome: str | None
    hypothesis_claim: str | None
    hypothesis_task: str | None
    official_evaluation: bool
    candidate_disposition: str
    perf_delta_pct: float | None


class _HypothesisFields(TypedDict, total=False):
    """Keyword fields of ``Hypothesis``, so helper overrides stay checked."""

    hypothesis_id: str
    plan: OrchestratorPlan
    started_round: int
    parent_round: int | None
    parent_commit: str | None
    rounds: list[RoundRecord]


def _round(number: int, **overrides: Unpack[_RoundFields]) -> RoundRecord:
    fields: _RoundFields = {
        "round_number": number,
        "commit": f"c{number}",
        "perf_metric": None,
        "perf_unit": None,
        "passed": False,
    }
    fields.update(overrides)
    return RoundRecord(**fields)


def _hypothesis(
    identifier: str, started_round: int, /, **overrides: Unpack[_HypothesisFields]
) -> Hypothesis:
    fields: _HypothesisFields = {
        "hypothesis_id": identifier,
        "plan": OrchestratorPlan(
            hypothesis_id=identifier,
            hypothesis=f"claim for {identifier}",
            task=f"test {identifier}",
            pass_criteria="",
            reasoning="",
        ),
        "started_round": started_round,
    }
    fields.update(overrides)
    return Hypothesis(**fields)


def _view(state: AgentRunState, *, run_id: str = "run-1") -> RunView:
    """Project a hand-built `AgentRunState` into the `RunView` `DesignLog.rounds` reads.

    `DesignLog.rounds` now consumes `vibesys.api`'s `RunView`, not raw core
    state directly (see `server.api.experiments._view`, the same fixture
    pattern), so a state fixture built by hand still needs this projection
    step before reaching it.
    """
    return project_run_view(
        state,
        run_id=run_id,
        status=RunStatus.ACTIVE,
        experiment_revision=state.experiment_revision,
    )


def _git(workspace: Path, *args: str) -> str:
    command = ["git", "-C", str(workspace), *args]
    result = run_test_command(command, capture_output=True, check=True, text=True)
    return result.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit_all(path: Path, message: str) -> str:
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", message)
    return _git(path, "rev-parse", "HEAD")


def _name_status(*fields: str) -> str:
    """Render NUL-delimited ``--name-status`` fields exactly as git emits them."""
    return "".join(f"{field}\0" for field in fields)


def _no_patch(_base: str, _head: str, _paths: tuple[str, ...]) -> str | None:
    """Patch reader stand-in for projections that never read patch text."""
    return None


def _tracked(workspace: Path) -> DesignLog:
    """Bind a projection to the real read-only git callables for *workspace*."""
    tracker = GitTracker(workspace, run_id="design-test", events=NullGitTrackerEvents())
    reader = WorkspacePatchReader(workspace, warning=NullGitTrackerEvents().warning)
    return DesignLog(workspace=workspace, diff=tracker.diff_name_status, patch=reader.diff_patch)


class _RecordingGitEvents(NullGitTrackerEvents):
    """Capture tracker warnings; the read-only tracker emits nothing else."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, summary: str, *, detail: str | None = None) -> None:
        self.warnings.append(summary if detail is None else f"{summary}: {detail}")


def test_design_log_derives_per_round_file_changes(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "workspace")
    (workspace / "src").mkdir()
    (workspace / "src" / "lib.rs").write_text("fn main() {}\n", encoding="utf-8")
    baseline = _commit_all(workspace, "baseline")

    (workspace / "src" / "lib.rs").write_text("fn main() { fast() }\n", encoding="utf-8")
    (workspace / "src" / "ffi.rs").write_text("pub fn fast() {}\n", encoding="utf-8")
    (workspace / ".vibesys" / "state").mkdir(parents=True)
    (workspace / ".vibesys" / "state" / "note").write_text("bookkeeping\n", encoding="utf-8")
    (workspace / "progress.md").write_text("round 1\n", encoding="utf-8")
    (workspace / "pareto-frontier.md").write_text("# Pareto frontier\n", encoding="utf-8")
    first = _commit_all(workspace, "round 1")

    (workspace / "src" / "ffi.rs").unlink()
    (workspace / "src" / "lib.rs").rename(workspace / "src" / "queue.rs")
    second = _commit_all(workspace, "round 2")

    state = AgentRunState(
        hypotheses=[
            _hypothesis(
                "H-01",
                1,
                parent_commit=baseline,
                rounds=[
                    _round(1, hypothesis_id="H-01", commit=first),
                    _round(2, hypothesis_id="H-01", commit=second),
                ],
            )
        ]
    )

    first_entry, second_entry = _tracked(workspace).rounds(_view(state), baseline=baseline)

    # Each entry publishes the same derived base its file list was diffed
    # against, so a patch query can only name ranges the log itself used.
    assert (first_entry.base, first_entry.commit) == (baseline, first)
    assert (second_entry.base, second_entry.commit) == (first, second)
    assert first_entry.files is not None
    assert sorted((change.change, change.path) for change in first_entry.files) == [
        ("added", "src/ffi.rs"),
        ("modified", "src/lib.rs"),
    ]
    assert second_entry.files is not None
    assert [(change.change, change.path, change.renamed_from) for change in second_entry.files] == [
        ("deleted", "src/ffi.rs", None),
        ("renamed", "src/queue.rs", "src/lib.rs"),
    ]


def test_design_log_publishes_only_the_round_and_its_files(tmp_path: Path) -> None:
    """Stage fields belong to the experiment log; the design log has no copy."""
    state = AgentRunState(
        hypotheses=[
            _hypothesis(
                "H-01",
                1,
                rounds=[_round(1, hypothesis_id="H-01", commit="a" * 40, judge_verdict="pass")],
            )
        ]
    )

    (entry,) = DesignLog(workspace=tmp_path, diff=lambda _base, _head: "", patch=_no_patch).rounds(
        _view(state), baseline="0" * 40
    )

    assert entry.model_dump() == {
        "round": 1,
        "commit": "a" * 40,
        "base": "0" * 40,
        "files": [],
    }


def test_design_log_measures_a_reverted_hypothesis_from_its_parent(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "workspace")
    (workspace / "queue.rs").write_text("original\n", encoding="utf-8")
    baseline = _commit_all(workspace, "baseline")

    (workspace / "queue.rs").write_text("attempt one\n", encoding="utf-8")
    first = _commit_all(workspace, "round 1")

    _git(workspace, "checkout", "--quiet", baseline, "--", "queue.rs")
    (workspace / "batching.rs").write_text("attempt two\n", encoding="utf-8")
    second = _commit_all(workspace, "round 2 from baseline")

    state = AgentRunState(
        hypotheses=[
            _hypothesis(
                "H-01",
                1,
                parent_commit=baseline,
                rounds=[_round(1, hypothesis_id="H-01", commit=first)],
            ),
            _hypothesis(
                "H-02",
                2,
                parent_commit=baseline,
                rounds=[_round(2, hypothesis_id="H-02", commit=second)],
            ),
        ]
    )

    _, second_entry = _tracked(workspace).rounds(_view(state), baseline=baseline)

    # Against round 1 the range would also claim queue.rs reverted; against
    # the hypothesis's own parent it is exactly the new file.
    assert second_entry.files is not None
    assert [(change.change, change.path) for change in second_entry.files] == [
        ("added", "batching.rs")
    ]


def test_design_log_leaves_unresolvable_ranges_unknown(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "workspace")
    state = AgentRunState(
        hypotheses=[
            _hypothesis(
                "H-01",
                1,
                rounds=[
                    _round(1, hypothesis_id="H-01", commit=None),
                    _round(2, hypothesis_id="H-01", commit="a" * 40),
                ],
            )
        ]
    )

    entries = _tracked(workspace).rounds(_view(state), baseline="0" * 40)

    # Round 1 recorded no checkpoint; round 2's range does not resolve in a
    # repository that never held those objects. Both stay None, never [].
    assert [entry.files for entry in entries] == [None, None]


def test_design_log_never_passes_a_non_hex_commit_to_git(tmp_path: Path) -> None:
    """A revision that could read as a git option must not reach the diff."""
    attempted: list[tuple[str, str]] = []

    def diff(base: str, head: str) -> str | None:
        attempted.append((base, head))
        return ""

    state = AgentRunState(
        hypotheses=[
            _hypothesis(
                "H-01",
                1,
                parent_commit="--output=escape",
                rounds=[_round(1, hypothesis_id="H-01", commit="HEAD~1")],
            )
        ]
    )

    entries = DesignLog(workspace=tmp_path, diff=diff, patch=_no_patch).rounds(
        _view(state), baseline="--upload-pack=sh"
    )

    assert attempted == []
    assert [entry.files for entry in entries] == [None]


def test_diff_name_status_rejects_a_revision_expression(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "workspace")
    tracker = GitTracker(workspace, run_id="design-test", events=NullGitTrackerEvents())

    with pytest.raises(ValueError, match="not a commit object name"):
        tracker.diff_name_status("HEAD~1", "HEAD")


@pytest.mark.parametrize(
    "failure", [OSError("git is missing"), subprocess.TimeoutExpired(["git"], 10.0)]
)
def test_diff_name_status_logs_a_failed_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    workspace = _repo(tmp_path / "workspace")
    events = _RecordingGitEvents()
    tracker = GitTracker(workspace, run_id="design-test", events=events)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(subprocess, "run", explode)

    assert tracker.diff_name_status("a" * 40, "b" * 40) is None
    assert [line for line in events.warnings if "read-only diff failed" in line]


def test_diff_name_status_logs_a_nonzero_exit(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "workspace")
    events = _RecordingGitEvents()
    tracker = GitTracker(workspace, run_id="design-test", events=events)

    assert tracker.diff_name_status("a" * 40, "b" * 40) is None
    assert [line for line in events.warnings if "read-only diff exit" in line]


def test_design_log_drops_a_rename_out_of_framework_memory(tmp_path: Path) -> None:
    """Both sides of a rename are filtered, not only the new path."""
    output = _name_status("R100", "progress/round-0001.md", "notes/round-1.md", "M", "src/lib.rs")

    state = AgentRunState(
        hypotheses=[
            _hypothesis(
                "H-01",
                1,
                parent_commit="a" * 40,
                rounds=[_round(1, hypothesis_id="H-01", commit="b" * 40)],
            )
        ]
    )

    (entry,) = DesignLog(
        workspace=tmp_path, diff=lambda _base, _head: output, patch=_no_patch
    ).rounds(_view(state), baseline="0" * 40)

    assert entry.files is not None
    assert [(change.change, change.path) for change in entry.files] == [("modified", "src/lib.rs")]


def test_design_log_caches_each_commit_range(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def diff(base: str, head: str) -> str | None:
        calls.append((base, head))
        return _name_status("M", "src/lib.rs")

    state = AgentRunState(
        hypotheses=[
            _hypothesis(
                "H-01",
                1,
                parent_commit="a" * 40,
                rounds=[
                    _round(1, hypothesis_id="H-01", commit="b" * 40),
                    _round(2, hypothesis_id="H-01", commit="c" * 40),
                ],
            )
        ]
    )
    design = DesignLog(workspace=tmp_path, diff=diff, patch=_no_patch)

    first = design.rounds(_view(state), baseline="0" * 40)
    second = design.rounds(_view(state), baseline="0" * 40)

    # Every range is immutable once its checkpoint exists, so the repeat
    # projection runs no git at all.
    assert len(calls) == 2
    assert [entry.files for entry in first] == [entry.files for entry in second]


def test_design_log_retries_a_failed_range(tmp_path: Path) -> None:
    """A transient git failure must not be cached as a permanent blank."""
    outputs: list[str | None] = [None, _name_status("A", "src/lib.rs")]

    def diff(_base: str, _head: str) -> str | None:
        return outputs.pop(0)

    state = AgentRunState(
        hypotheses=[
            _hypothesis(
                "H-01",
                1,
                parent_commit="a" * 40,
                rounds=[_round(1, hypothesis_id="H-01", commit="b" * 40)],
            )
        ]
    )
    design = DesignLog(workspace=tmp_path, diff=diff, patch=_no_patch)

    (failed,) = design.rounds(_view(state), baseline="0" * 40)
    (recovered,) = design.rounds(_view(state), baseline="0" * 40)

    assert failed.files is None
    assert recovered.files is not None
    assert [change.path for change in recovered.files] == ["src/lib.rs"]


def test_design_log_evicts_oldest_ranges_past_capacity(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def diff(base: str, head: str) -> str | None:
        calls.append((base, head))
        return ""

    design = DesignLog(workspace=tmp_path, diff=diff, patch=_no_patch, capacity=1)
    state = AgentRunState(
        hypotheses=[
            _hypothesis(
                "H-01",
                1,
                parent_commit="a" * 40,
                rounds=[
                    _round(1, hypothesis_id="H-01", commit="b" * 40),
                    _round(2, hypothesis_id="H-01", commit="c" * 40),
                ],
            )
        ]
    )

    design.rounds(_view(state), baseline="0" * 40)
    design.rounds(_view(state), baseline="0" * 40)

    # Capacity 1 holds only the newest range, so each pass evicts the entry
    # the next pass asks for first and every range runs again.
    assert len(calls) == 4


def test_design_patch_covers_every_change_kind(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "workspace")
    (workspace / "lib.rs").write_text("fn main() {}\n", encoding="utf-8")
    (workspace / "ffi.rs").write_text("pub fn fast() {}\n", encoding="utf-8")
    (workspace / "gone.rs").write_text("obsolete\n", encoding="utf-8")
    base = _commit_all(workspace, "baseline")

    (workspace / "lib.rs").write_text("fn main() { fast() }\n", encoding="utf-8")
    (workspace / "new.rs").write_text("pub fn newer() {}\n", encoding="utf-8")
    (workspace / "gone.rs").unlink()
    (workspace / "ffi.rs").rename(workspace / "ffi2.rs")
    head = _commit_all(workspace, "round 1")

    design = _tracked(workspace)

    modified = design.patch(base, head, "lib.rs")
    assert modified.patch is not None
    assert "-fn main() {}" in modified.patch
    assert "+fn main() { fast() }" in modified.patch

    added = design.patch(base, head, "new.rs")
    assert added.patch is not None
    assert "new file mode" in added.patch
    assert "+pub fn newer() {}" in added.patch

    deleted = design.patch(base, head, "gone.rs")
    assert deleted.patch is not None
    assert "deleted file mode" in deleted.patch
    assert "-obsolete" in deleted.patch

    renamed = design.patch(base, head, "ffi2.rs")
    assert renamed.renamed_from == "ffi.rs"
    assert renamed.patch is not None
    assert "rename from ffi.rs" in renamed.patch
    assert "rename to ffi2.rs" in renamed.patch

    assert not any(entry.truncated for entry in (modified, added, deleted, renamed))


def test_design_patch_truncates_at_the_size_bound(tmp_path: Path) -> None:
    """An oversized diff is cut on a line boundary and flagged, never silent."""
    workspace = _repo(tmp_path / "workspace")
    (workspace / "table.rs").write_text("start\n", encoding="utf-8")
    base = _commit_all(workspace, "baseline")
    lines = "".join(f"const ROW_{index}: u32 = {index};\n" for index in range(12_000))
    (workspace / "table.rs").write_text(lines, encoding="utf-8")
    head = _commit_all(workspace, "round 1")

    result = _tracked(workspace).patch(base, head, "table.rs")

    assert result.truncated is True
    assert result.patch is not None
    assert len(result.patch) <= _PATCH_CHAR_LIMIT
    # The cut lands on a line boundary, so the tail line is never garbled.
    assert result.patch.endswith("\n")


def test_design_patch_rejects_a_revision_expression(tmp_path: Path) -> None:
    design = DesignLog(workspace=tmp_path, diff=lambda _base, _head: "", patch=_no_patch)

    with pytest.raises(ValueError, match="not a commit object name"):
        design.patch("HEAD~1", "a" * 40, "lib.rs")
    with pytest.raises(ValueError, match="not a commit object name"):
        design.patch("a" * 40, "--output=escape", "lib.rs")


def test_design_patch_rejects_paths_outside_the_filtered_change_list(tmp_path: Path) -> None:
    """Framework paths and unknown paths never reach the git command line."""
    attempted: list[tuple[str, ...]] = []

    def patch(_base: str, _head: str, paths: tuple[str, ...]) -> str | None:
        attempted.append(paths)
        return ""

    output = _name_status("M", "src/lib.rs", "M", "progress.md")
    design = DesignLog(workspace=tmp_path, diff=lambda _base, _head: output, patch=patch)

    with pytest.raises(ValueError, match="not in the round's change list"):
        design.patch("a" * 40, "b" * 40, "progress.md")
    with pytest.raises(ValueError, match="not in the round's change list"):
        design.patch("a" * 40, "b" * 40, "unknown.rs")
    assert attempted == []


def test_design_patch_reads_a_rename_with_both_paths(tmp_path: Path) -> None:
    """Both sides go into the pathspec, or git would report a bare delete."""
    attempted: list[tuple[str, ...]] = []

    def patch(_base: str, _head: str, paths: tuple[str, ...]) -> str | None:
        attempted.append(paths)
        return "diff --git a/old.rs b/new.rs\n"

    output = _name_status("R100", "old.rs", "new.rs")
    design = DesignLog(workspace=tmp_path, diff=lambda _base, _head: output, patch=patch)

    result = design.patch("a" * 40, "b" * 40, "new.rs")

    assert attempted == [("old.rs", "new.rs")]
    assert result.renamed_from == "old.rs"


def test_design_patch_caches_successes_and_retries_failures(tmp_path: Path) -> None:
    outputs: list[str | None] = [None, "diff --git a/lib.rs b/lib.rs\n"]
    calls: list[tuple[str, ...]] = []

    def patch(_base: str, _head: str, paths: tuple[str, ...]) -> str | None:
        calls.append(paths)
        return outputs.pop(0)

    output = _name_status("M", "lib.rs")
    design = DesignLog(workspace=tmp_path, diff=lambda _base, _head: output, patch=patch)

    failed = design.patch("a" * 40, "b" * 40, "lib.rs")
    recovered = design.patch("a" * 40, "b" * 40, "lib.rs")
    repeated = design.patch("a" * 40, "b" * 40, "lib.rs")

    # The failure is reported, not cached; the success is cached, so the
    # third read runs no git at all.
    assert failed.patch is None
    assert recovered.patch is not None
    assert repeated.patch == recovered.patch
    assert len(calls) == 2


def test_design_patch_degrades_when_the_file_list_is_unreadable(tmp_path: Path) -> None:
    """No file list means nothing to validate against and nothing to read."""
    attempted: list[tuple[str, ...]] = []

    def patch(_base: str, _head: str, paths: tuple[str, ...]) -> str | None:
        attempted.append(paths)
        return ""

    design = DesignLog(workspace=tmp_path, diff=lambda _base, _head: None, patch=patch)

    result = design.patch("a" * 40, "b" * 40, "lib.rs")

    assert result.patch is None
    assert result.truncated is False
    assert attempted == []


def _configuration() -> AgentRunConfiguration:
    return AgentRunConfiguration(
        outer_loop="agent",
        inner_loop="single-agent",
        interface="inprocess",
        agent_backend="stub",
        compute_backend="cpu",
        profiler="none",
        max_rounds=3,
        max_retries_per_round=1,
        judge_every=1,
        official_eval_every=1,
        memory_layout="files",
        run_environment=RunEnvironmentRecord(name="local"),
    )


def _project_run(project: Path, *, trusted_input_baseline: str) -> tuple[Project, str]:
    vibesys_project = Project.open(project)
    vibesys_project.state.create_project("queue")
    manifest = vibesys_project.state.new_run_manifest(
        "queue",
        run_id="queue-run",
        branch="vibesys/queue-run",
        vibesys_version="0.2.0-test",
        configuration=_configuration(),
        trusted_input_baseline=trusted_input_baseline,
    )
    vibesys_project.state.create_run(manifest)
    return vibesys_project, manifest.run_id


def test_service_builds_design_from_workspace_history(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "project")
    (workspace / "OBJECTIVE.md").write_text("Make the queue fast.\n", encoding="utf-8")
    baseline = _commit_all(workspace, "baseline")
    (workspace / "ring.rs").write_text("ring buffer\n", encoding="utf-8")
    first = _commit_all(workspace, "round 1")

    project, run_id = _project_run(workspace, trusted_input_baseline=baseline)
    portable = project.state.portable_namespace(run_id, "agent")
    AgentRunStateStore(portable).save(
        AgentRunState(
            hypotheses=[
                _hypothesis(
                    "H-01",
                    1,
                    rounds=[_round(1, hypothesis_id="H-01", commit=first, passed=True)],
                )
            ]
        )
    )
    parts = build_server_parts(project.state.log_directory(run_id), project=project, run_id=run_id)

    response = parts.api.execute(DesignQuery())

    assert response.design_ready is True
    (entry,) = response.design
    assert entry.round == 1
    assert entry.commit == first
    assert entry.files is not None
    assert [(change.change, change.path) for change in entry.files] == [("added", "ring.rs")]


def test_service_serves_patches_for_published_design_ranges(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "project")
    (workspace / "OBJECTIVE.md").write_text("Make the queue fast.\n", encoding="utf-8")
    baseline = _commit_all(workspace, "baseline")
    (workspace / "ring.rs").write_text("ring buffer\n", encoding="utf-8")
    first = _commit_all(workspace, "round 1")

    project, run_id = _project_run(workspace, trusted_input_baseline=baseline)
    AgentRunStateStore(project.state.portable_namespace(run_id, "agent")).save(
        AgentRunState(
            hypotheses=[
                _hypothesis("H-01", 1, rounds=[_round(1, hypothesis_id="H-01", commit=first)])
            ],
        )
    )
    parts = build_server_parts(project.state.log_directory(run_id), project=project, run_id=run_id)

    (entry,) = parts.api.execute(DesignQuery()).design
    assert (entry.base, entry.commit) == (baseline, first)
    # The client echoes the published range back; use the same values here.
    response = parts.api.execute(DesignPatchQuery(base=baseline, head=first, path="ring.rs"))

    assert response.ok is True
    patch = response.design_patch
    assert patch is not None
    assert patch.truncated is False
    assert patch.patch is not None
    assert "+ring buffer" in patch.patch


def test_service_reports_no_design_patch_before_attach(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path / "logs")

    response = parts.api.execute(DesignPatchQuery(base="a" * 40, head="b" * 40, path="ring.rs"))

    assert response.ok is True
    assert response.design_patch is None


def test_service_reuses_one_design_projection_per_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated public queries reuse the same immutable commit-range diff."""
    workspace = _repo(tmp_path / "project")
    (workspace / "OBJECTIVE.md").write_text("Make the queue fast.\n", encoding="utf-8")
    baseline = _commit_all(workspace, "baseline")
    (workspace / "ring.rs").write_text("ring buffer\n", encoding="utf-8")
    first = _commit_all(workspace, "round 1")

    project, run_id = _project_run(workspace, trusted_input_baseline=baseline)
    AgentRunStateStore(project.state.portable_namespace(run_id, "agent")).save(
        AgentRunState(
            hypotheses=[
                _hypothesis("H-01", 1, rounds=[_round(1, hypothesis_id="H-01", commit=first)])
            ],
        )
    )
    constructions = 0
    diff_calls = 0

    class _CountingGitTracker(GitTracker):
        def __init__(self, root: Path, *, run_id: str, events: GitTrackerEvents) -> None:
            nonlocal constructions
            constructions += 1
            super().__init__(root, run_id=run_id, events=events)

        def diff_name_status(self, base: str, head: str) -> str | None:
            nonlocal diff_calls
            diff_calls += 1
            return super().diff_name_status(base, head)

    monkeypatch.setattr(service_module, "GitTracker", _CountingGitTracker)
    parts = build_server_parts(project.state.log_directory(run_id), project=project, run_id=run_id)

    first = parts.api.execute(DesignQuery())
    second = parts.api.execute(DesignQuery())

    assert first.design == second.design
    assert constructions == 1
    assert diff_calls == 1


def test_service_reports_design_not_ready_before_attach(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path / "logs")

    response = parts.api.execute(DesignQuery())

    assert response.design == []
    assert response.design_ready is False
