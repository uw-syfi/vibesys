"""Ending a session: the agent is stopped, the socket is what it promises."""

from __future__ import annotations

import os
import stat
import tempfile
import threading
from pathlib import Path

import protovalidate
import pytest
from agentshim import AssistantText
from hypothesis import given
from hypothesis import strategies as st

from tests import helpers as h
from tests.fakes import FaultyFiles, FileFaultError, ScriptedDriver, TurnScript
from vs_sidecar.engine import Engine
from vs_sidecar.gen.vibesys.orchestrator.v1 import agent_session_pb2 as sc
from vs_sidecar.holder import SessionProcessesFile
from vs_sidecar.main import agent_groups, child_pids, work
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


def _read(path: Path) -> sc.SessionProcesses:
    return sc.SessionProcesses.FromString(path.read_bytes())


def test_the_record_names_the_live_groups_and_is_emptied_when_the_agent_is_closed(
    tmp_path: Path,
) -> None:
    live = [41, 42]
    path = tmp_path / "processes.pb"
    groups = SessionProcessesFile(path, 7, lambda: list(live))
    groups.record()
    assert _read(path) == sc.SessionProcesses(holder_pid=7, agent_process_groups=[41, 42])
    live.append(43)  # the provider respawned the agent mid-turn
    groups.record()
    assert _read(path).agent_process_groups == [41, 42, 43]
    groups.clear()
    assert _read(path) == sc.SessionProcesses(holder_pid=7)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["processes.pb"]


@given(live=st.lists(st.integers(min_value=-5, max_value=2**32 - 1), max_size=6))
def test_the_record_is_always_one_the_host_accepts(live: list[int]) -> None:
    with tempfile.TemporaryDirectory(prefix="vs") as directory:
        path = Path(directory) / "processes.pb"
        SessionProcessesFile(path, 7, lambda: live).record()
        record = _read(path)
        assert protovalidate.validate(record) is None
        assert record.holder_pid == 7
        assert list(record.agent_process_groups) == [group for group in live if group > 1]


def test_without_a_proc_the_children_come_from_the_process_table(tmp_path: Path) -> None:
    # The form `ps -A -o pid=,ppid=` prints on macOS: padded columns, the parent last.
    table = f"    1     0\n  300   {os.getpid()}\n  301   300\n  ignored\n{os.getpid() + 1:>5} {os.getpid()}\n"
    assert child_pids(tmp_path, lambda: table) == sorted([300, os.getpid() + 1])


def test_a_proc_that_lists_no_children_is_not_trusted_to_have_none(tmp_path: Path) -> None:
    (tmp_path / "self" / "task" / "1").mkdir(parents=True)  # no `children` file: an older kernel
    table = f"  300   {os.getpid()}\n"
    assert child_pids(tmp_path, lambda: table) == [300]


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
