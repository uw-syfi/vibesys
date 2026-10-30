"""The Stop hook: it holds a session only while a run is active and before its deadline.

The hook is a standalone program, so its public surface is `main` (the hook protocol: JSON in,
a block decision or nothing out) and `block_reason`. The clock and the fallback directory are
arguments, so no test waits or reads the real time.
"""

from __future__ import annotations

import importlib.util
import io
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    type NewRepo = Callable[[], Path]

HOOK = Path(__file__).resolve().parents[1] / "deadline_stop.py"
NOW = datetime(2026, 5, 6, 7, 8, 9, tzinfo=timezone.utc)


def _load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("deadline_stop_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


@pytest.fixture(scope="session")
def new_repo(tmp_path_factory: pytest.TempPathFactory) -> NewRepo:
    """A factory of empty repositories; per example, so a property starts from nothing."""
    return lambda: tmp_path_factory.mktemp("repo")


def start_run(repo: Path, *, deadline: str, run_id: str = "run-1", active: bool = True) -> None:
    run_dir = repo / ".vibesys-optimize" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"task": "demo", "deadline": deadline}))
    if active:
        (repo / ".vibesys-optimize" / "active").write_text(run_id + "\n")


def stop(repo: Path, *, payload: str | None = None) -> tuple[int, str]:
    out = io.StringIO()
    event = (
        json.dumps({"cwd": str(repo), "hook_event_name": "Stop"}) if payload is None else payload
    )
    code = hook.main(io.StringIO(event), out, now=lambda: NOW, fallback_cwd=repo)
    return code, out.getvalue()


def at(seconds_from_now: int) -> str:
    return (NOW + timedelta(seconds=seconds_from_now)).isoformat()


@given(extra=st.lists(st.sampled_from(["run-1", "notes", "other"]), unique=True, max_size=3))
def test_a_session_without_an_active_run_is_never_held(new_repo: NewRepo, extra: list[str]) -> None:
    repo = new_repo()
    for name in extra:
        (repo / ".vibesys-optimize" / name).mkdir(parents=True)
    start_run(repo, deadline=at(3600), run_id="finished", active=False)
    assert stop(repo) == (0, "")


@given(seconds=st.integers(min_value=1, max_value=10**6))
@example(seconds=1)
@example(seconds=60)
@example(seconds=61)
def test_before_the_deadline_the_stop_is_blocked_and_the_minutes_left_are_counted_up(
    new_repo: NewRepo, seconds: int
) -> None:
    repo = new_repo()
    start_run(repo, deadline=at(seconds))
    code, out = stop(repo)
    assert code == 0
    decision = json.loads(out)
    assert decision["decision"] == "block"
    minutes = math.ceil(seconds / 60)
    assert decision["reason"].startswith(f"{minutes} minute")
    assert ".vibesys-optimize/run-1/notes" in decision["reason"]
    assert "remove .vibesys-optimize/active" in decision["reason"]


@given(seconds=st.integers(min_value=-(10**6), max_value=0))
@example(seconds=0)
def test_at_and_after_the_deadline_the_stop_goes_through(new_repo: NewRepo, seconds: int) -> None:
    repo = new_repo()
    start_run(repo, deadline=at(seconds))
    assert stop(repo) == (0, "")


def test_the_reason_reads_right_for_one_minute_and_for_many(new_repo: NewRepo) -> None:
    repo = new_repo()
    start_run(repo, deadline=at(30))
    assert json.loads(stop(repo)[1])["reason"].startswith("1 minute remains until the deadline:")
    (repo / ".vibesys-optimize" / "run-1" / "run.json").write_text(
        json.dumps({"task": "demo", "deadline": at(5400)})
    )
    assert json.loads(stop(repo)[1])["reason"].startswith("90 minutes remain until the deadline:")


def test_removing_the_active_file_at_wrap_up_releases_the_session(new_repo: NewRepo) -> None:
    repo = new_repo()
    start_run(repo, deadline=at(600))
    assert stop(repo)[1] != ""
    (repo / ".vibesys-optimize" / "active").unlink()
    assert stop(repo) == (0, "")


def test_a_deadline_in_another_timezone_is_the_same_instant(new_repo: NewRepo) -> None:
    repo = new_repo()
    plus_nine = timezone(timedelta(hours=9))
    start_run(repo, deadline=(NOW + timedelta(minutes=10)).astimezone(plus_nine).isoformat())
    assert json.loads(stop(repo)[1])["reason"].startswith("10 minutes remain")


def test_a_deadline_written_with_z_is_utc(new_repo: NewRepo) -> None:
    repo = new_repo()
    start_run(repo, deadline=(NOW + timedelta(minutes=7)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert json.loads(stop(repo)[1])["reason"].startswith("7 minutes remain")


@pytest.mark.parametrize(
    "run_json",
    [
        None,
        "not json",
        "[]",
        "{}",
        '{"deadline": 5}',
        '{"deadline": null}',
        '{"deadline": "tomorrow"}',
        '{"deadline": "2999-01-01T00:00:00"}',
    ],
)
def test_unreadable_run_state_allows_the_stop(new_repo: NewRepo, run_json: str | None) -> None:
    repo = new_repo()
    start_run(repo, deadline=at(600))
    run_file = repo / ".vibesys-optimize" / "run-1" / "run.json"
    if run_json is None:
        run_file.unlink()
    else:
        run_file.write_text(run_json)
    assert stop(repo) == (0, "")


@pytest.mark.parametrize("run_id", ["", "  \n", "..", ".", "../run-1", "a/b", "a\\b", "missing"])
def test_an_active_file_that_names_no_run_allows_the_stop(new_repo: NewRepo, run_id: str) -> None:
    repo = new_repo()
    start_run(repo, deadline=at(600))
    (repo / ".vibesys-optimize" / "active").write_text(run_id)
    assert stop(repo) == (0, "")


@given(payload=st.text(max_size=40))
@example(payload="")
@example(payload="not json")
@example(payload="[]")
@example(payload='{"cwd": 5}')
def test_any_stdin_gives_exit_zero_and_never_a_malformed_answer(
    new_repo: NewRepo, payload: str
) -> None:
    repo = new_repo()
    start_run(repo, deadline=at(600))
    code, out = stop(repo, payload=payload)
    assert code == 0
    if out:
        assert json.loads(out)["decision"] == "block"


def test_the_event_names_the_directory_and_its_absence_means_the_process_directory(
    new_repo: NewRepo,
) -> None:
    held, free = new_repo(), new_repo()
    start_run(held, deadline=at(600))
    out = io.StringIO()
    event = json.dumps({"cwd": str(free)})
    assert hook.main(io.StringIO(event), out, now=lambda: NOW, fallback_cwd=held) == 0
    assert out.getvalue() == ""
    out = io.StringIO()
    assert hook.main(io.StringIO("{}"), out, now=lambda: NOW, fallback_cwd=held) == 0
    assert json.loads(out.getvalue())["decision"] == "block"


@given(seconds=st.integers(min_value=1, max_value=10**6))
@example(seconds=600)
@pytest.mark.regression
def test_a_repeated_stop_hook_continuation_is_released(new_repo: NewRepo, seconds: int) -> None:
    repo = new_repo()
    start_run(repo, deadline=at(seconds))
    assert stop(repo, payload=json.dumps({"cwd": str(repo), "stop_hook_active": True})) == (0, "")


@pytest.mark.parametrize("pending", ["background_tasks", "session_crons"])
def test_pending_work_can_wake_the_lead_without_a_block(new_repo: NewRepo, pending: str) -> None:
    repo = new_repo()
    start_run(repo, deadline=at(600))
    assert stop(repo, payload=json.dumps({"cwd": str(repo), pending: [{"id": "task"}]})) == (0, "")
