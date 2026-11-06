"""Ending a session: the agent is stopped, the socket is what it promises."""

from __future__ import annotations

import os
import stat
import tempfile
import threading
from pathlib import Path

import pytest
from agentshim import AssistantText

from tests import helpers as h
from tests.fakes import FaultyFiles, FileFaultError, ScriptedDriver, TurnScript
from vs_sidecar.engine import Engine
from vs_sidecar.main import AgentGroups, agent_groups, child_pids, work
from vs_sidecar.server import Wakeup, serve
from vs_sidecar.store import FileStore, MemoryStore


def _engine(driver: ScriptedDriver) -> tuple[Engine, MemoryStore]:
    store = MemoryStore()
    engine = Engine(h.REF, store, lambda _sink: driver)
    engine.start()
    return engine, store


def test_shutdown_during_a_turn_interrupts_it_stops_the_agent_and_closes_the_session() -> None:
    driver = ScriptedDriver([TurnScript([AssistantText("x", "a1")])])
    engine, store = _engine(driver)
    driver.at_start = engine.shutdown
    engine.send(h.start_turn("e1", "t1"))
    engine.run_next()
    kinds = h.kinds(store.events_after(0))
    assert kinds[-2:] == ["turn_ended", "closed"]
    assert driver.interrupts == ["t1"]
    assert driver.closed


def test_release_stops_the_agent_and_writes_nothing() -> None:
    driver = ScriptedDriver()
    engine, store = _engine(driver)
    before = store.events_after(0)
    engine.release()
    assert driver.closed
    assert store.events_after(0) == before


def test_the_children_of_this_process_are_read_from_proc(tmp_path: Path) -> None:
    for task, children in {"1": "30 10", "2": "", "3": "20"}.items():
        directory = tmp_path / "self" / "task" / task
        directory.mkdir(parents=True)
        (directory / "children").write_text(children)
    assert child_pids(tmp_path) == [10, 20, 30]
    # None of these exist as processes here, so none leads a group this process can name.
    assert agent_groups(tmp_path) == []


def test_the_socket_is_for_the_owner_alone_whatever_the_umask() -> None:
    previous = os.umask(0)
    try:
        with tempfile.TemporaryDirectory(prefix="vs") as directory:
            engine, _ = _engine(ScriptedDriver())
            server = serve(engine, Wakeup(), Path(directory) / "s.sock")
            try:
                mode = stat.S_IMODE((Path(directory) / "s.sock").stat().st_mode)
                names = sorted(p.name for p in Path(directory).iterdir())
                # Binding leaves the umask of the whole process alone.
                held = os.umask(0)
            finally:
                server.stop(grace=0).wait()
    finally:
        os.umask(previous)
    assert mode & 0o077 == 0, oct(mode)
    assert names == ["s.sock"]
    assert held == 0


def test_agent_groups_names_the_live_groups_and_is_emptied_when_the_agent_is_closed(
    tmp_path: Path,
) -> None:
    live = [41, 42]
    path = tmp_path / "agent.pgids"
    groups = AgentGroups(path, lambda: list(live))
    groups.record()
    assert path.read_text() == "41\n42\n"
    live.append(43)  # the provider respawned the agent mid-turn
    groups.record()
    assert path.read_text() == "41\n42\n43\n"
    groups.clear()
    assert path.read_text() == ""


def test_a_store_failure_on_a_command_thread_is_raised_by_the_worker_and_never_closes_in_memory() -> (
    None
):
    files = FaultyFiles(fail_at=3)  # 1: the opened event, 2: the close's effect id, 3: closed
    driver = ScriptedDriver()
    engine = Engine(h.REF, FileStore(files), lambda _sink: driver)
    engine.start()
    with pytest.raises(FileFaultError):
        engine.send(h.close("e1"))
    assert not engine.finished
    with pytest.raises(FileFaultError):
        work(engine, Wakeup(), threading.Event())
