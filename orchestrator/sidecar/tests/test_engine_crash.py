"""The engine over files that crash at every append: a restart reports a log that extends what was shown."""

from __future__ import annotations

import pytest
from agentshim import AssistantText, ToolCall

from tests import helpers as h
from tests.fakes import CrashError, FaultyFiles, ScriptedDriver, TurnScript
from vs_sidecar.engine import Engine
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.store import FileStore


def _turn() -> TurnScript:
    return TurnScript([ToolCall("c1", "Bash", None), AssistantText("hi", "a1")])


def _run(files: FaultyFiles) -> list:
    """Run one turn until the files crash; return what a follower saw."""
    engine = Engine(h.launch(), FileStore(files), lambda _sink: ScriptedDriver([_turn()]))
    try:
        engine.start()
        engine.send(h.start_turn("e1", "t1"))
        engine.run_next()
    except CrashError:
        pass
    return engine.events_after(0)


def _appends_without_a_crash() -> int:
    files = FaultyFiles()
    _run(files)
    return files.appends


@pytest.mark.parametrize("keep", [0, 3, 1000])
@pytest.mark.parametrize("at", range(1, _appends_without_a_crash() + 1))
def test_a_crash_at_every_write_is_reported_by_the_next_sidecar_without_changing_history(
    at: int, keep: int
) -> None:
    files = FaultyFiles(crash_at=at, keep=keep)
    shown = _run(files)
    survived = FileStore(files.reopened()).events_after(0)
    second = Engine(h.launch(), FileStore(files.reopened()), lambda _sink: ScriptedDriver())
    second.start()
    events = second.events_after(0)
    assert events[: len(shown)] == shown
    assert [e.offset for e in events] == list(range(1, len(events) + 1))
    items: dict[str, int] = {}
    for event in events:
        if event.HasField("item"):
            items[event.item.id] = event.item.status
    assert orch.ITEM_STATUS_RUNNING not in items.values()
    # A launch that left nothing on disk starts afresh; one that left history is reported lost.
    if survived:
        assert events[-1].HasField("failed")
