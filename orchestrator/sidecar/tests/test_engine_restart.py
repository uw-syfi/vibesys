"""A sidecar that dies mid-stream: the next one reports what was lost and never replays a command."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agentshim import AssistantText, ToolCall

from tests import helpers as h
from tests.fakes import CrashError, ScriptedDriver, TurnScript
from vs_sidecar.engine import Engine
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.store import FileStore

if TYPE_CHECKING:
    from pathlib import Path


def _engine(path: Path, driver: ScriptedDriver) -> Engine:
    engine = Engine(h.launch(), FileStore(path), lambda _sink: driver)
    engine.start()
    return engine


def test_a_restart_in_the_middle_of_a_turn_ends_it_failed_and_reports_the_session_lost(
    tmp_path: Path,
) -> None:
    first = _engine(
        tmp_path,
        ScriptedDriver(
            [TurnScript([AssistantText("part", "a1"), ToolCall("c1", "Bash", None), CrashError])]
        ),
    )
    first.send(h.start_turn("e1", "t1"))
    with pytest.raises(CrashError):
        first.run_next()
    before = FileStore(tmp_path).events_after(0)
    assert h.kinds(before) == ["opened", "item", "item"]

    second = _engine(tmp_path, ScriptedDriver())
    events = second.events_after(0)
    assert events[: len(before)] == before
    tail = events[len(before) :]
    assert h.kinds(tail) == ["turn_ended", "failed"]
    assert tail[0].turn_ended.turn_id == "t1"
    assert tail[0].turn_ended.status == orch.TURN_STATUS_FAILED
    assert [e.offset for e in events] == list(range(1, len(events) + 1))


def test_a_command_the_daemon_sends_again_after_the_restart_is_dropped(tmp_path: Path) -> None:
    first = _engine(tmp_path, ScriptedDriver([TurnScript([CrashError])]))
    command = h.start_turn("e1", "t1")
    first.send(command)
    with pytest.raises(CrashError):
        first.run_next()
    second = _engine(tmp_path, ScriptedDriver([TurnScript([AssistantText("late", "a1")])]))
    second.send(command)
    assert second.run_next() is False
    assert "item" not in h.kinds(second.events_after(0))


def test_a_restarted_session_takes_no_new_turns_but_closes(tmp_path: Path) -> None:
    _engine(tmp_path, ScriptedDriver())
    second = _engine(tmp_path, ScriptedDriver([TurnScript([AssistantText("x", "a1")])]))
    second.send(h.start_turn("e2", "t2"))
    assert second.run_next() is False
    second.send(h.close("e3"))
    assert h.kinds(second.events_after(0))[-2:] == ["failed", "closed"]


def test_a_restart_twice_reports_the_loss_once(tmp_path: Path) -> None:
    _engine(tmp_path, ScriptedDriver())
    _engine(tmp_path, ScriptedDriver())
    third = _engine(tmp_path, ScriptedDriver())
    assert h.kinds(third.events_after(0)).count("failed") == 1


def test_a_closed_session_stays_closed_across_a_restart(tmp_path: Path) -> None:
    first = _engine(tmp_path, ScriptedDriver())
    first.send(h.close("e1"))
    second = _engine(tmp_path, ScriptedDriver())
    assert second.finished
    assert h.kinds(second.events_after(0)) == ["opened", "closed"]


def test_the_follower_resumes_from_the_last_offset_it_committed(tmp_path: Path) -> None:
    first = _engine(
        tmp_path, ScriptedDriver([TurnScript([AssistantText("a", "a1"), AssistantText("b", "a2")])])
    )
    first.send(h.start_turn("e1", "t1"))
    first.run_next()
    everything = first.events_after(0)
    second = _engine(tmp_path, ScriptedDriver())
    for committed in range(len(everything) + 1):
        assert (
            second.events_after(committed)[: len(everything) - committed] == everything[committed:]
        )
