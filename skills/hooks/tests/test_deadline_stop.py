"""Stop protocol decision table, using explicit time and state, no runtime tasks."""

from __future__ import annotations

import importlib.util
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

spec = importlib.util.spec_from_file_location(
    "deadline_stop", Path(__file__).resolve().parents[1] / "deadline_stop.py"
)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("window", ["search", "wrap-up", "deadline"])
@pytest.mark.parametrize("kind", ["none", "cron", "subagent", "tracked", "untracked", "completed"])
@pytest.mark.parametrize("run_state", ["active", "finished", "missing", "corrupt"])
@pytest.mark.parametrize(
    "choices", [(a, b, c) for a in (False, True) for b in (False, True) for c in (False, True)]
)
def test_stop_decision_table(
    tmp_path: Path,
    window: str,
    kind: str,
    run_state: str,
    choices: tuple[bool, bool, bool],
) -> None:
    continuation, idle, full = choices
    run = tmp_path / ".vibesys-optimize/run-1"
    run.mkdir(parents=True)
    (run.parent / "active").write_text("run-1")
    state = {
        "schema": 1,
        "session_id": "lead",
        "idle_reason": "host busy" if idle else None,
        "decisions": [],
        "run_id": "run-1",
        "task": "demo",
        "started_at": NOW.isoformat(),
        "base_commit": "a" * 40,
        "main_branch": "vibesys/run-1",
        "main_commit": "a" * 40,
        "main_moved_at": NOW.isoformat(),
        "pending_move": None,
        "promotions": [],
        "deadline": (NOW + timedelta(seconds=1800)).isoformat(),
        "wrap_up_at": (NOW + timedelta(seconds=1350)).isoformat(),
        "items": {str(i): {"state": "dispatched"} for i in range(3)} if full else {},
        "fanout": 3,
        "finished": run_state == "finished",
        "commands": {"task": "measurement"},
    }
    (run / "run.json").write_text(json.dumps(state) if run_state != "corrupt" else "bad")
    if run_state == "missing":
        (run.parent / "active").unlink()
    event = {
        "cwd": str(run),
        "session_id": "lead",
        "stop_hook_active": continuation,
        "session_crons": [{"id": "wakeup"}] if kind == "cron" else [],
        "background_tasks": [],
    }
    if kind in {"subagent", "tracked", "untracked", "completed"}:
        event["background_tasks"] = [
            {
                "id": "other" if kind == "untracked" else "task",
                "type": "subagent" if kind == "subagent" else "shell",
                "status": "completed" if kind == "completed" else "running",
            }
        ]
    out = io.StringIO()
    now = NOW + timedelta(seconds={"search": 100, "wrap-up": 1350, "deadline": 1800}[window])
    assert (
        hook.main(io.StringIO(json.dumps(event)), out, now=lambda: now, fallback_cwd=tmp_path) == 0
    )
    blocked = (
        run_state == "active"
        and window != "deadline"
        and not (window == "search" and (idle or full))
    )
    assert bool(out.getvalue()) == blocked
    if blocked:
        assert json.loads(out.getvalue())["reason"] == hook.status(state, now)["next"]


def test_cron_alone_cannot_release_unexplained_slots(tmp_path: Path) -> None:
    test_stop_decision_table(tmp_path, "search", "cron", "active", choices=(False, False, False))


@pytest.mark.parametrize("payload", ["[]", "null", "invalid", "{}"])
def test_bad_input_fails_open(tmp_path: Path, payload: str) -> None:
    out = io.StringIO()
    assert hook.main(io.StringIO(payload), out, now=lambda: NOW, fallback_cwd=tmp_path) == 0
    assert out.getvalue() == ""


@given(choices=st.tuples(st.booleans(), st.booleans(), st.booleans()))
def test_search_gate_requires_a_reason_for_free_slots(
    tmp_path_factory: pytest.TempPathFactory, choices: tuple[bool, bool, bool]
) -> None:
    test_stop_decision_table(tmp_path_factory.mktemp("gate"), "search", "none", "active", choices)


@pytest.mark.parametrize("bound", [None, "lead", "other"])
def test_hook_binds_only_first_session_and_releases_other_sessions(
    tmp_path: Path, bound: str | None
) -> None:
    test_stop_decision_table(tmp_path, "search", "none", "active", choices=(False, False, False))
    run = tmp_path / ".vibesys-optimize/run-1"
    state = json.loads((run / "run.json").read_text())
    state["session_id"] = bound
    state["wrap_up_at"] = state["wrap_up_at"].replace("+00:00", "Z")
    (run / "run.json").write_text(json.dumps(state))
    reason = hook.block_reason(tmp_path, NOW, {"session_id": "lead"})
    assert (reason is None) == (bound == "other")
    assert json.loads((run / "run.json").read_text())["session_id"] == (bound or "lead")
