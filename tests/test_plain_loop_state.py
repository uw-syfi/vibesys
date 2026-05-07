"""Tests for the issue-loop state machine checkpoint and resume logic."""

import json

import pytest

from vibeserve_agent.loops.plain.loop import (
    PlainLoopState,
    _determine_resume_point,
    _load_state,
    _save_state,
)
from vibeserve_agent.loops.plain.issue_board import IssueStatus, IssueBoard, IssueType


def _make_store(tmp_path) -> IssueBoard:
    return IssueBoard(tmp_path / "issues.json")


# ---------------------------------------------------------------------------
# _save_state / _load_state round-trip
# ---------------------------------------------------------------------------


class TestSaveLoadState:
    def test_round_trip(self, tmp_path):
        state = PlainLoopState(
            round_idx=2,
            phase="judge",
            current_issue_id=5,
            bootstrap_done=True,
        )
        _save_state(tmp_path, state)
        loaded = _load_state(tmp_path)
        assert loaded is not None
        assert loaded.round_idx == 2
        assert loaded.phase == "judge"
        assert loaded.current_issue_id == 5
        assert loaded.bootstrap_done is True

    def test_load_missing(self, tmp_path):
        assert _load_state(tmp_path) is None

    def test_load_corrupt_json(self, tmp_path):
        (tmp_path / "state.json").write_text("not json", encoding="utf-8")
        assert _load_state(tmp_path) is None

    def test_load_wrong_version(self, tmp_path):
        (tmp_path / "state.json").write_text(
            json.dumps({"version": 999, "iteration": 0}), encoding="utf-8"
        )
        assert _load_state(tmp_path) is None

    def test_atomic_write_leaves_no_tmp(self, tmp_path):
        _save_state(tmp_path, PlainLoopState())
        assert not (tmp_path / "state.json.tmp").exists()
        assert (tmp_path / "state.json").exists()

    def test_load_ignores_extra_fields(self, tmp_path):
        data = {
            "version": 1,
            "round_idx": 3,
            "phase": "implementer",
            "current_issue_id": None,
            "bootstrap_done": True,
            "extra_field": "ignored",
        }
        (tmp_path / "state.json").write_text(json.dumps(data), encoding="utf-8")
        loaded = _load_state(tmp_path)
        assert loaded is not None
        assert loaded.round_idx == 3
        assert loaded.bootstrap_done is True


# ---------------------------------------------------------------------------
# _determine_resume_point
# ---------------------------------------------------------------------------


class TestDetermineResumePoint:
    def test_none_state_starts_fresh(self, tmp_path):
        store = _make_store(tmp_path)
        i, phase, issue_id = _determine_resume_point(None, store)
        assert i == 0
        assert phase == "implementer"
        assert issue_id is None

    def test_mid_implementer_re_runs_implementer(self, tmp_path):
        store = _make_store(tmp_path)
        issue = store.create(
            type=IssueType.BUG, title="t", description="d",
            created_by="x", iteration=1,
        )
        store.update_status(issue.id, IssueStatus.IN_PROGRESS,
                             actor="loop", iteration=1)
        state = PlainLoopState(round_idx=0, phase="implementer",
                               current_issue_id=issue.id, bootstrap_done=True)
        i, phase, issue_id = _determine_resume_point(state, store)
        assert i == 0
        assert phase == "implementer"
        assert issue_id == issue.id

    def test_mid_judge_re_runs_judge(self, tmp_path):
        store = _make_store(tmp_path)
        issue = store.create(
            type=IssueType.BUG, title="t", description="d",
            created_by="x", iteration=1,
        )
        store.update_status(issue.id, IssueStatus.IN_PROGRESS,
                             actor="loop", iteration=1)
        state = PlainLoopState(round_idx=0, phase="judge",
                               current_issue_id=issue.id, bootstrap_done=True)
        i, phase, issue_id = _determine_resume_point(state, store)
        assert i == 0
        assert phase == "judge"
        assert issue_id == issue.id

    def test_resume_picks_drain_when_open_issues_remain(self, tmp_path):
        store = _make_store(tmp_path)
        store.create(type=IssueType.BUG, title="t", description="d",
                     created_by="x", iteration=1)
        # Loop was past judge, no current_issue_id
        state = PlainLoopState(round_idx=1, phase="implementer",
                               current_issue_id=None, bootstrap_done=True)
        i, phase, issue_id = _determine_resume_point(state, store)
        assert i == 1
        assert phase == "implementer"
        assert issue_id is None

    def test_resume_goes_to_implementer_when_no_open_issues(self, tmp_path):
        # When no open issues remain, _determine_resume_point returns
        # phase="implementer" — the drain loop in run_plain_loop will
        # immediately exit (next_open() is None) and fall through to
        # perf_eval naturally, so the function never needs to "request"
        # perf_eval explicitly.
        store = _make_store(tmp_path)
        issue = store.create(type=IssueType.BUG, title="t", description="d",
                             created_by="x", iteration=1)
        store.update_status(issue.id, IssueStatus.CLOSED,
                             actor="judge", iteration=1)
        state = PlainLoopState(round_idx=1, phase="implementer",
                               current_issue_id=None, bootstrap_done=True)
        i, phase, issue_id = _determine_resume_point(state, store)
        assert phase == "implementer"
        assert issue_id is None

    def test_resume_after_perf_eval_with_no_open_issues(self, tmp_path):
        """If we crashed during/after perf_eval with no open issues left,
        _determine_resume_point should return phase='implementer'. The
        drain loop will then immediately exit (next_open() is None) and
        fall through to a fresh perf_eval naturally."""
        store = _make_store(tmp_path)
        closed = store.create(type=IssueType.BUG, title="t", description="d",
                               created_by="x", iteration=1)
        store.update_status(closed.id, IssueStatus.CLOSED,
                             actor="judge", iteration=1)
        state = PlainLoopState(round_idx=2, phase="perf_eval",
                               current_issue_id=None, bootstrap_done=True)
        i, phase, issue_id = _determine_resume_point(state, store)
        assert i == 2
        assert phase == "implementer"
        assert issue_id is None

    def test_resume_skips_stale_current_issue_id_if_already_closed(self, tmp_path):
        # If state.current_issue_id points at an issue that the store says is
        # already CLOSED (race after a crash), we should NOT try to re-run
        # that phase. Instead, drain remaining open issues.
        store = _make_store(tmp_path)
        closed = store.create(type=IssueType.BUG, title="closed", description="d",
                              created_by="x", iteration=1)
        store.update_status(closed.id, IssueStatus.CLOSED,
                             actor="judge", iteration=1)
        store.create(type=IssueType.BUG, title="open", description="d",
                     created_by="x", iteration=1)
        state = PlainLoopState(round_idx=0, phase="judge",
                               current_issue_id=closed.id, bootstrap_done=True)
        i, phase, issue_id = _determine_resume_point(state, store)
        # Should NOT try to re-run judge on the closed issue
        assert not (phase == "judge" and issue_id == closed.id)
        assert phase == "implementer"
