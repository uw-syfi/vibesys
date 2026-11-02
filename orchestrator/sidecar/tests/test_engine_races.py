"""Interrupts and steers that race a turn: before the provider starts it, and after it is done.

``ScriptedDriver`` pauses the engine at those two points (``at_start``, ``at_end``) and the test
sends its command from there, so each interleaving happens exactly, with no thread and no wait.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshim import AssistantText
from hypothesis import given
from hypothesis import strategies as st

from tests import helpers as h
from tests.fakes import ScriptedDriver, TurnScript
from vs_sidecar.engine import Engine
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.store import MemoryStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sidecar.gen.vibesys.orchestrator.v1 import sidecar_pb2 as sc


def _engine(driver: ScriptedDriver) -> tuple[Engine, MemoryStore]:
    store = MemoryStore()
    engine = Engine(h.launch(), store, lambda _sink: driver)
    engine.start()
    return engine, store


def _ends(events: list[sc.SidecarEvent]) -> dict[str, int]:
    return {e.turn_ended.turn_id: e.turn_ended.status for e in events if e.HasField("turn_ended")}


def _quick(text: str = "x") -> TurnScript:
    return TurnScript([AssistantText(text, f"a-{text}")], on_steer=[])


def test_an_interrupt_that_comes_after_the_turn_began_and_before_the_provider_started_it_ends_it() -> (
    None
):
    driver = ScriptedDriver([_quick()])
    engine, store = _engine(driver)
    driver.at_start = lambda: engine.send(h.interrupt("e2", "t1"))
    engine.send(h.start_turn("e1", "t1"))
    engine.run_next()
    events = store.events_after(0)
    assert _ends(events) == {"t1": orch.TURN_STATUS_INTERRUPTED}
    assert "item" not in h.kinds(events)
    assert driver.interrupts == ["t1"]


def test_an_interrupt_that_comes_when_the_provider_is_done_and_the_engine_does_not_know_yet_is_dropped_and_leaves_the_next_turn_alone() -> (
    None
):
    driver = ScriptedDriver([_quick("a"), _quick("b")])
    engine, store = _engine(driver)
    driver.at_end = lambda: engine.send(h.interrupt("e3", "t1"))
    engine.send(h.start_turn("e1", "t1"))
    engine.send(h.start_turn("e2", "t2"))
    engine.run_next()
    driver.at_end = None
    engine.run_next()
    assert _ends(store.events_after(0)) == {
        "t1": orch.TURN_STATUS_COMPLETED,
        "t2": orch.TURN_STATUS_COMPLETED,
    }
    assert driver.ignored_interrupts == ["t1"]


def test_an_interrupt_for_a_turn_that_ended_never_reaches_the_driver() -> None:
    driver = ScriptedDriver([_quick("a"), _quick("b")])
    engine, store = _engine(driver)
    engine.send(h.start_turn("e1", "t1"))
    engine.run_next()
    engine.send(h.start_turn("e2", "t2"))
    engine.send(h.interrupt("e3", "t1"))
    engine.run_next()
    assert _ends(store.events_after(0))["t2"] == orch.TURN_STATUS_COMPLETED
    assert driver.interrupts == []
    assert driver.ignored_interrupts == []


def test_an_interrupt_for_a_queued_turn_ends_it_interrupted_instead_of_losing_it() -> None:
    driver = ScriptedDriver([_quick("a")])
    engine, store = _engine(driver)
    engine.send(h.start_turn("e1", "t1"))
    engine.send(h.interrupt("e2", "t1"))
    assert engine.run_next() is False
    assert _ends(store.events_after(0)) == {"t1": orch.TURN_STATUS_INTERRUPTED}


def test_a_steer_that_comes_before_the_provider_started_the_turn_is_answered_once_as_refused() -> (
    None
):
    driver = ScriptedDriver([TurnScript([AssistantText("x", "a1")], on_steer=[])])
    engine, store = _engine(driver)
    driver.at_start = lambda: engine.send(h.steer("e2", "t1", "m1"))
    engine.send(h.start_turn("e1", "t1"))
    engine.run_next()
    outcomes = [e.steer for e in store.events_after(0) if e.HasField("steer")]
    assert [(o.message_id, o.accepted) for o in outcomes] == [("m1", False)]
    assert driver.steers == []


def test_a_steer_that_comes_when_the_provider_is_done_is_answered_once_and_never_reaches_the_next_turn() -> (
    None
):
    driver = ScriptedDriver([_quick("a"), _quick("b")])
    engine, store = _engine(driver)
    driver.at_end = lambda: engine.send(h.steer("e3", "t1", "m1"))
    engine.send(h.start_turn("e1", "t1"))
    engine.send(h.start_turn("e2", "t2"))
    engine.run_next()
    driver.at_end = None
    engine.run_next()
    outcomes = [e.steer for e in store.events_after(0) if e.HasField("steer")]
    assert [(o.message_id, o.accepted) for o in outcomes] == [("m1", False)]
    assert driver.steers == []


@given(
    start_ops=st.lists(
        st.tuples(st.sampled_from(["interrupt", "steer"]), st.sampled_from(["t1", "t2"])),
        max_size=4,
    ),
    end_ops=st.lists(
        st.tuples(st.sampled_from(["interrupt", "steer"]), st.sampled_from(["t1", "t2"])),
        max_size=4,
    ),
)
def test_commands_sent_around_turn_one_end_it_or_cancel_turn_two_only_as_addressed(
    start_ops: list[tuple[str, str]], end_ops: list[tuple[str, str]]
) -> None:
    driver = ScriptedDriver([_quick("a"), _quick("b")])
    engine, store = _engine(driver)
    counter = iter(range(1000))

    def sender(ops: list[tuple[str, str]]) -> Callable[[], None]:
        def send() -> None:
            for kind, turn in ops:
                n = next(counter)
                if kind == "interrupt":
                    engine.send(h.interrupt(f"x{n}", turn))
                else:
                    engine.send(h.steer(f"x{n}", turn, f"m{n}"))

        return send

    driver.at_start, driver.at_end = sender(start_ops), sender(end_ops)
    engine.send(h.start_turn("e1", "t1"))
    engine.send(h.start_turn("e2", "t2"))
    engine.run_next()
    driver.at_start = driver.at_end = None
    engine.run_next()
    events = store.events_after(0)
    sent = start_ops + end_ops
    ends = _ends(events)
    # Turn one stops only for an interrupt sent while the provider had it; turn two is cancelled
    # only for an interrupt that named it, and otherwise runs to its end.
    assert ends["t1"] == (
        orch.TURN_STATUS_INTERRUPTED
        if ("interrupt", "t1") in start_ops
        else orch.TURN_STATUS_COMPLETED
    )
    assert ends["t2"] == (
        orch.TURN_STATUS_INTERRUPTED if ("interrupt", "t2") in sent else orch.TURN_STATUS_COMPLETED
    )
    # Every steer is answered once, and none reached a driver that was not running its turn.
    answered = [e.steer.message_id for e in events if e.HasField("steer")]
    assert sorted(answered) == sorted(
        f"m{n}" for n, (kind, _) in enumerate(start_ops + end_ops) if kind == "steer"
    )
    assert driver.steers == []
