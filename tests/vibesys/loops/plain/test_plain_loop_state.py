"""Tests for plain-loop resume decisions over typed state."""

from pathlib import Path

from vibesys.loops.plain.loop import PlainLoopState, _determine_resume_point
from vs_issue_board.api import IssueBoard, IssueStatus, IssueType


def _make_store(tmp_path: Path) -> IssueBoard:
    return IssueBoard(tmp_path / "issues.json")


class TestDetermineResumePoint:
    def test_none_state_starts_fresh(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        i, phase, issue_id = _determine_resume_point(None, store)
        assert i == 0
        assert phase == "implementer"
        assert issue_id is None

    def test_mid_implementer_re_runs_implementer(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        issue = store.create(
            type=IssueType.BUG,
            title="t",
            description="d",
            created_by="x",
            iteration=1,
        )
        store.update_status(issue.id, IssueStatus.IN_PROGRESS, actor="loop", iteration=1)
        state = PlainLoopState(
            round_idx=0, phase="implementer", current_issue_id=issue.id, bootstrap_done=True
        )
        i, phase, issue_id = _determine_resume_point(state, store)
        assert i == 0
        assert phase == "implementer"
        assert issue_id == issue.id

    def test_mid_judge_re_runs_judge(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        issue = store.create(
            type=IssueType.BUG,
            title="t",
            description="d",
            created_by="x",
            iteration=1,
        )
        store.update_status(issue.id, IssueStatus.IN_PROGRESS, actor="loop", iteration=1)
        state = PlainLoopState(
            round_idx=0, phase="judge", current_issue_id=issue.id, bootstrap_done=True
        )
        i, phase, issue_id = _determine_resume_point(state, store)
        assert i == 0
        assert phase == "judge"
        assert issue_id == issue.id

    def test_resume_picks_drain_when_open_issues_remain(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.create(type=IssueType.BUG, title="t", description="d", created_by="x", iteration=1)
        # Loop was past judge, no current_issue_id
        state = PlainLoopState(
            round_idx=1, phase="implementer", current_issue_id=None, bootstrap_done=True
        )
        i, phase, issue_id = _determine_resume_point(state, store)
        assert i == 1
        assert phase == "implementer"
        assert issue_id is None

    def test_resume_goes_to_implementer_when_no_open_issues(self, tmp_path: Path) -> None:
        # When no open issues remain, _determine_resume_point returns
        # phase="implementer" — the drain loop in run_plain_loop will
        # immediately exit (next_open() is None) and fall through to
        # perf_eval naturally, so the function never needs to "request"
        # perf_eval explicitly.
        store = _make_store(tmp_path)
        issue = store.create(
            type=IssueType.BUG, title="t", description="d", created_by="x", iteration=1
        )
        store.update_status(issue.id, IssueStatus.CLOSED, actor="judge", iteration=1)
        state = PlainLoopState(
            round_idx=1, phase="implementer", current_issue_id=None, bootstrap_done=True
        )
        _i, phase, issue_id = _determine_resume_point(state, store)
        assert phase == "implementer"
        assert issue_id is None

    def test_resume_after_perf_eval_with_no_open_issues(self, tmp_path: Path) -> None:
        """If we crashed during/after perf_eval with no open issues left,
        _determine_resume_point should return phase='implementer'. The
        drain loop will then immediately exit (next_open() is None) and
        fall through to a fresh perf_eval naturally."""
        store = _make_store(tmp_path)
        closed = store.create(
            type=IssueType.BUG, title="t", description="d", created_by="x", iteration=1
        )
        store.update_status(closed.id, IssueStatus.CLOSED, actor="judge", iteration=1)
        state = PlainLoopState(
            round_idx=2, phase="perf_eval", current_issue_id=None, bootstrap_done=True
        )
        i, phase, issue_id = _determine_resume_point(state, store)
        assert i == 2
        assert phase == "implementer"
        assert issue_id is None

    def test_resume_skips_stale_current_issue_id_if_already_closed(self, tmp_path: Path) -> None:
        # If state.current_issue_id points at an issue that the store says is
        # already CLOSED (race after a crash), we should NOT try to re-run
        # that phase. Instead, drain remaining open issues.
        store = _make_store(tmp_path)
        closed = store.create(
            type=IssueType.BUG, title="closed", description="d", created_by="x", iteration=1
        )
        store.update_status(closed.id, IssueStatus.CLOSED, actor="judge", iteration=1)
        store.create(type=IssueType.BUG, title="open", description="d", created_by="x", iteration=1)
        state = PlainLoopState(
            round_idx=0, phase="judge", current_issue_id=closed.id, bootstrap_done=True
        )
        _i, phase, issue_id = _determine_resume_point(state, store)
        # Should NOT try to re-run judge on the closed issue
        assert not (phase == "judge" and issue_id == closed.id)
        assert phase == "implementer"
