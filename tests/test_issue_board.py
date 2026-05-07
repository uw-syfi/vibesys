"""Tests for the issue-loop persistent issue tracker (issues.json)."""

import json
import time
from unittest.mock import Mock

import pytest

from vibeserve_agent.loops.plain.issue_board import (
    Issue,
    IssueEvent,
    IssueStatus,
    IssueBoard,
    IssueType,
)


def _make_store(tmp_path) -> IssueBoard:
    return IssueBoard(tmp_path / "issues.json")


def _create(store, *, type=IssueType.BUG, title="t", description="d",
            created_by="perf_eval", iteration=1) -> Issue:
    return store.create(
        type=type, title=title, description=description,
        created_by=created_by, iteration=iteration,
    )


# ---------------------------------------------------------------------------
# create + persistence
# ---------------------------------------------------------------------------


class TestCreate:
    def test_create_assigns_sequential_ids(self, tmp_path):
        store = _make_store(tmp_path)
        a = _create(store, title="a")
        b = _create(store, title="b")
        c = _create(store, title="c")
        assert (a.id, b.id, c.id) == (1, 2, 3)

    def test_create_persists_atomically_to_disk(self, tmp_path):
        store = _make_store(tmp_path)
        _create(store, title="persist me")
        # Reload from disk via a fresh store
        fresh = _make_store(tmp_path)
        issues = fresh.list()
        assert len(issues) == 1
        assert issues[0].title == "persist me"
        # No tmp file left behind
        assert not (tmp_path / "issues.json.tmp").exists()

    def test_create_records_create_event_in_history(self, tmp_path):
        store = _make_store(tmp_path)
        issue = _create(store, created_by="perf_eval", iteration=2)
        assert len(issue.history) == 1
        evt = issue.history[0]
        assert evt.actor == "perf_eval"
        assert evt.action == "create"
        assert evt.iteration == 2

    def test_create_accepts_string_type(self, tmp_path):
        store = _make_store(tmp_path)
        issue = store.create(
            type="perf", title="p", description="d",
            created_by="perf_eval", iteration=1,
        )
        assert issue.type == IssueType.PERF


class TestLoadCorrupt:
    def test_load_corrupt_json_starts_empty(self, tmp_path):
        path = tmp_path / "issues.json"
        path.write_text("not json", encoding="utf-8")
        store = IssueBoard(path)
        assert store.list() == []

    def test_load_wrong_version_starts_empty(self, tmp_path):
        path = tmp_path / "issues.json"
        path.write_text(json.dumps({"version": 999, "next_id": 1, "issues": []}),
                        encoding="utf-8")
        store = IssueBoard(path)
        assert store.list() == []


class TestReload:
    def test_reload_picks_up_external_writes(self, tmp_path):
        store_a = _make_store(tmp_path)
        store_b = _make_store(tmp_path)
        _create(store_a, title="written by a")
        # store_b is unaware of the new issue
        assert len(store_b.list()) == 0
        store_b.reload()
        assert len(store_b.list()) == 1
        assert store_b.list()[0].title == "written by a"


# ---------------------------------------------------------------------------
# list / filter
# ---------------------------------------------------------------------------


class TestList:
    def test_list_filters_by_status(self, tmp_path):
        store = _make_store(tmp_path)
        a = _create(store, title="a")
        b = _create(store, title="b")
        store.update_status(a.id, IssueStatus.CLOSED, actor="judge", iteration=1)
        opens = store.list(status=IssueStatus.OPEN)
        closeds = store.list(status=IssueStatus.CLOSED)
        assert {i.id for i in opens} == {b.id}
        assert {i.id for i in closeds} == {a.id}

    def test_list_filters_by_type(self, tmp_path):
        store = _make_store(tmp_path)
        _create(store, type=IssueType.BUG, title="bug")
        _create(store, type=IssueType.PERF, title="perf")
        bugs = store.list(type=IssueType.BUG)
        perfs = store.list(type=IssueType.PERF)
        assert len(bugs) == 1 and bugs[0].title == "bug"
        assert len(perfs) == 1 and perfs[0].title == "perf"

    def test_list_accepts_string_filters(self, tmp_path):
        store = _make_store(tmp_path)
        _create(store, type=IssueType.BUG)
        bugs = store.list(status="open", type="bug")
        assert len(bugs) == 1


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_substring_case_insensitive(self, tmp_path):
        store = _make_store(tmp_path)
        _create(store, title="Add Paged KV cache", description="Reduce frag")
        _create(store, title="Other thing", description="unrelated")
        hits = store.search("paged kv")
        assert len(hits) == 1
        assert hits[0].title == "Add Paged KV cache"

    def test_search_keyword_and_match(self, tmp_path):
        store = _make_store(tmp_path)
        _create(store, title="Paged KV cache", description="memory frag")
        _create(store, title="Continuous batching", description="paged scheduler")
        hits = store.search("paged, kv")
        # "paged" matches both, "kv" matches only the first
        assert len(hits) == 1
        assert hits[0].title == "Paged KV cache"

    def test_search_empty_query_returns_empty(self, tmp_path):
        store = _make_store(tmp_path)
        _create(store)
        assert store.search("") == []
        assert store.search("   ") == []
        assert store.search(",,") == []

    def test_search_no_match(self, tmp_path):
        store = _make_store(tmp_path)
        _create(store, title="hello", description="world")
        assert store.search("xyzzy") == []


# ---------------------------------------------------------------------------
# update_status / increment_attempts
# ---------------------------------------------------------------------------


class TestStateTransitions:
    def test_update_status_records_history_event(self, tmp_path):
        store = _make_store(tmp_path)
        issue = _create(store)
        store.update_status(issue.id, IssueStatus.IN_PROGRESS,
                             actor="loop", iteration=1, note="claimed")
        reloaded = store.get(issue.id)
        assert reloaded.status == IssueStatus.IN_PROGRESS
        assert len(reloaded.history) == 2
        assert reloaded.history[-1].action == "open->in_progress"
        assert reloaded.history[-1].note == "claimed"

    def test_update_status_to_closed_records_closed_iter(self, tmp_path):
        store = _make_store(tmp_path)
        issue = _create(store, iteration=1)
        store.update_status(issue.id, IssueStatus.IN_PROGRESS,
                             actor="loop", iteration=2)
        store.update_status(issue.id, IssueStatus.CLOSED,
                             actor="judge", iteration=2, note="passed")
        reloaded = store.get(issue.id)
        assert reloaded.status == IssueStatus.CLOSED
        assert reloaded.closed_iter == 2

    def test_update_status_blocked_records_closed_iter(self, tmp_path):
        store = _make_store(tmp_path)
        issue = _create(store, iteration=1)
        store.update_status(issue.id, IssueStatus.BLOCKED,
                             actor="loop", iteration=3, note="exhausted retries")
        reloaded = store.get(issue.id)
        assert reloaded.status == IssueStatus.BLOCKED
        assert reloaded.closed_iter == 3

    def test_increment_attempts_appends_event(self, tmp_path):
        store = _make_store(tmp_path)
        issue = _create(store)
        assert issue.attempts == 0
        store.increment_attempts(issue.id, actor="implementer",
                                  iteration=1, note="first try")
        store.increment_attempts(issue.id, actor="implementer",
                                  iteration=1, note="retry")
        reloaded = store.get(issue.id)
        assert reloaded.attempts == 2
        assert sum(1 for e in reloaded.history if e.action == "attempt") == 2

    def test_update_unknown_id_raises(self, tmp_path):
        store = _make_store(tmp_path)
        with pytest.raises(KeyError):
            store.update_status(999, IssueStatus.CLOSED, actor="x", iteration=1)
        with pytest.raises(KeyError):
            store.increment_attempts(999, actor="x", iteration=1)


# ---------------------------------------------------------------------------
# open_count_by_creator_in_iter (per-iteration cap helper)
# ---------------------------------------------------------------------------


class TestCapHelper:
    def test_open_count_by_creator_in_iter_counts_only_creator_and_iter(self, tmp_path):
        store = _make_store(tmp_path)
        _create(store, created_by="perf_eval", iteration=1)
        _create(store, created_by="perf_eval", iteration=1)
        _create(store, created_by="perf_eval", iteration=2)
        _create(store, created_by="judge", iteration=1)
        assert store.open_count_by_creator_in_iter("perf_eval", 1) == 2
        assert store.open_count_by_creator_in_iter("perf_eval", 2) == 1
        assert store.open_count_by_creator_in_iter("judge", 1) == 1
        assert store.open_count_by_creator_in_iter("nobody", 1) == 0

    def test_cap_helper_counts_closed_issues_too(self, tmp_path):
        # Closing an issue mid-iteration must NOT free up cap budget.
        store = _make_store(tmp_path)
        a = _create(store, created_by="perf_eval", iteration=1)
        store.update_status(a.id, IssueStatus.CLOSED,
                             actor="judge", iteration=1)
        assert store.open_count_by_creator_in_iter("perf_eval", 1) == 1


# ---------------------------------------------------------------------------
# next_open ordering
# ---------------------------------------------------------------------------


class TestNextOpen:
    def test_next_open_returns_none_when_empty(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.next_open() is None

    def test_next_open_returns_none_when_all_closed(self, tmp_path):
        store = _make_store(tmp_path)
        a = _create(store)
        store.update_status(a.id, IssueStatus.CLOSED,
                             actor="judge", iteration=1)
        assert store.next_open() is None

    def test_next_open_orders_bug_before_feature_before_perf(self, tmp_path):
        store = _make_store(tmp_path)
        # Insert in reverse priority order
        perf = _create(store, type=IssueType.PERF, title="perf")
        feature = _create(store, type=IssueType.FEATURE, title="feature")
        bug = _create(store, type=IssueType.BUG, title="bug")
        first = store.next_open()
        assert first.id == bug.id
        store.update_status(bug.id, IssueStatus.CLOSED, actor="x", iteration=1)
        second = store.next_open()
        assert second.id == feature.id
        store.update_status(feature.id, IssueStatus.CLOSED, actor="x", iteration=1)
        third = store.next_open()
        assert third.id == perf.id

    def test_next_open_fifo_within_type(self, tmp_path):
        store = _make_store(tmp_path)
        a = _create(store, type=IssueType.BUG, title="first bug")
        # Ensure a strict timestamp gap so created_at sort is stable
        time.sleep(0.005)
        b = _create(store, type=IssueType.BUG, title="second bug")
        time.sleep(0.005)
        c = _create(store, type=IssueType.BUG, title="third bug")
        assert store.next_open().id == a.id
        store.update_status(a.id, IssueStatus.CLOSED, actor="x", iteration=1)
        assert store.next_open().id == b.id
        store.update_status(b.id, IssueStatus.CLOSED, actor="x", iteration=1)
        assert store.next_open().id == c.id

    def test_next_open_skips_in_progress(self, tmp_path):
        store = _make_store(tmp_path)
        a = _create(store, type=IssueType.BUG, title="claimed")
        b = _create(store, type=IssueType.BUG, title="open")
        store.update_status(a.id, IssueStatus.IN_PROGRESS,
                             actor="loop", iteration=1)
        first = store.next_open()
        assert first.id == b.id


# ---------------------------------------------------------------------------
# IssueEvent.payload + on_change callback
# ---------------------------------------------------------------------------


class TestEventPayload:
    def test_payload_round_trip_through_disk(self, tmp_path):
        store = _make_store(tmp_path)
        issue = _create(store, title="payload test")
        payload = {
            "summary": "built x",
            "files_touched": ["a.py", "b.py"],
            "self_check": "ok",
        }
        store.increment_attempts(
            issue.id, actor="implementer", iteration=1,
            note="first try", payload=payload,
        )
        # Reload from disk via a fresh store
        fresh = _make_store(tmp_path)
        reloaded = fresh.get(issue.id)
        assert reloaded is not None
        last = reloaded.history[-1]
        assert last.action == "attempt"
        assert last.payload == payload

    def test_payload_default_none_for_existing_call_sites(self, tmp_path):
        store = _make_store(tmp_path)
        issue = _create(store)
        store.update_status(
            issue.id, IssueStatus.IN_PROGRESS,
            actor="loop", iteration=1, note="claimed",
        )
        store.increment_attempts(
            issue.id, actor="implementer", iteration=1, note="ran",
        )
        reloaded = store.get(issue.id)
        # create + update_status + increment_attempts = 3 events
        assert len(reloaded.history) == 3
        for evt in reloaded.history:
            assert evt.payload is None

    def test_load_old_json_without_payload_key(self, tmp_path):
        """A pre-feature issues.json whose history events lack the payload
        key must still load cleanly with payload=None defaults."""
        path = tmp_path / "issues.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "next_id": 2,
                    "issues": [
                        {
                            "id": 1,
                            "type": "bug",
                            "title": "legacy",
                            "description": "no payload here",
                            "status": "open",
                            "created_by": "perf_eval",
                            "created_iter": 1,
                            "created_at": "2026-01-01T00:00:00",
                            "updated_at": "2026-01-01T00:00:00",
                            "attempts": 0,
                            "history": [
                                {
                                    "timestamp": "2026-01-01T00:00:00",
                                    "actor": "perf_eval",
                                    "action": "create",
                                    "iteration": 1,
                                    "note": "",
                                    # NOTE: no "payload" key
                                }
                            ],
                            "closed_iter": None,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        store = IssueBoard(path)
        issue = store.get(1)
        assert issue is not None
        assert len(issue.history) == 1
        assert issue.history[0].payload is None


class TestReopenBlocked:
    def test_reopens_blocked_issue_resets_attempts_and_status(self, tmp_path):
        store = _make_store(tmp_path)
        a = _create(store, title="will block", iteration=1)
        store.increment_attempts(a.id, actor="implementer", iteration=1)
        store.increment_attempts(a.id, actor="implementer", iteration=1)
        store.update_status(
            a.id, IssueStatus.BLOCKED,
            actor="loop", iteration=1, note="exhausted",
        )
        before = store.get(a.id)
        assert before.status == IssueStatus.BLOCKED
        assert before.attempts == 2
        assert before.closed_iter == 1

        reopened = store.reopen_blocked(
            actor="loop:resume", iteration=2, note="retried on resume",
        )
        assert reopened == [a.id]

        after = store.get(a.id)
        assert after.status == IssueStatus.OPEN
        assert after.attempts == 0
        assert after.closed_iter is None
        last = after.history[-1]
        assert last.action == "blocked->open"
        assert last.actor == "loop:resume"
        assert last.iteration == 2
        assert last.note == "retried on resume"

    def test_reopen_blocked_only_touches_blocked_issues(self, tmp_path):
        store = _make_store(tmp_path)
        blocked = _create(store, title="blocked")
        open_issue = _create(store, title="open")
        closed = _create(store, title="closed")
        store.update_status(
            blocked.id, IssueStatus.BLOCKED,
            actor="loop", iteration=1,
        )
        store.update_status(
            closed.id, IssueStatus.CLOSED,
            actor="judge", iteration=1,
        )

        reopened = store.reopen_blocked(actor="loop:resume", iteration=2)
        assert reopened == [blocked.id]
        assert store.get(blocked.id).status == IssueStatus.OPEN
        assert store.get(open_issue.id).status == IssueStatus.OPEN
        assert store.get(closed.id).status == IssueStatus.CLOSED

    def test_reopen_blocked_no_blocked_is_noop(self, tmp_path):
        store = _make_store(tmp_path)
        _create(store)
        cb = Mock()
        store._on_change = cb  # bypass constructor wiring for this assertion
        reopened = store.reopen_blocked(actor="loop:resume", iteration=1)
        assert reopened == []
        # No mutation -> no callback fired (and importantly no save).
        assert cb.call_count == 0

    def test_reopen_blocked_persists_to_disk(self, tmp_path):
        store = _make_store(tmp_path)
        a = _create(store)
        store.update_status(a.id, IssueStatus.BLOCKED, actor="loop", iteration=1)
        store.reopen_blocked(actor="loop:resume", iteration=2)

        fresh = _make_store(tmp_path)
        reloaded = fresh.get(a.id)
        assert reloaded.status == IssueStatus.OPEN
        assert reloaded.attempts == 0
        assert reloaded.closed_iter is None
        assert reloaded.history[-1].action == "blocked->open"

    def test_reopen_blocked_picked_by_next_open(self, tmp_path):
        store = _make_store(tmp_path)
        a = _create(store, type=IssueType.BUG)
        store.update_status(a.id, IssueStatus.BLOCKED, actor="loop", iteration=1)
        assert store.next_open() is None  # blocked, not picked
        store.reopen_blocked(actor="loop:resume", iteration=2)
        nxt = store.next_open()
        assert nxt is not None
        assert nxt.id == a.id


class TestOnChangeCallback:
    def test_callback_fires_on_each_mutation(self, tmp_path):
        cb = Mock()
        store = IssueBoard(tmp_path / "issues.json", on_change=cb)
        # Bootstrap save in __init__ must NOT fire the callback.
        assert cb.call_count == 0

        issue = _create(store)
        assert cb.call_count == 1

        store.update_status(
            issue.id, IssueStatus.IN_PROGRESS,
            actor="loop", iteration=1,
        )
        assert cb.call_count == 2

        store.increment_attempts(
            issue.id, actor="implementer", iteration=1,
        )
        assert cb.call_count == 3

    def test_callback_failure_does_not_corrupt_store(self, tmp_path):
        path = tmp_path / "issues.json"
        cb = Mock(side_effect=RuntimeError("renderer exploded"))
        store = IssueBoard(path, on_change=cb)
        # The mutation must still succeed even though the callback raises.
        issue = _create(store, title="resilient")
        assert cb.call_count == 1
        # JSON file is consistent and contains the new issue
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data["issues"]) == 1
        assert data["issues"][0]["title"] == "resilient"
        # Reloading via a fresh store still works
        fresh = _make_store(tmp_path)
        assert fresh.get(issue.id) is not None
