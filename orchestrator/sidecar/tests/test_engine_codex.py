"""The engine over a scripted Codex app-server: turns, items that never complete, usage, steers."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest
from agentshim.testing import AwaitSteer, CodexScript, Leave, RunCommand, Say, Spend, Think

from tests import helpers as h
from vs_sidecar.engine import Engine, InvalidCommandError
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.store import MemoryStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentshim import AgentEvent

    from vs_sidecar.driver import Driver, TurnResult


def _engine(script: CodexScript, store: MemoryStore | None = None) -> tuple[Engine, MemoryStore]:
    store = store if store is not None else MemoryStore()
    engine = Engine(h.launch(), store, h.codex_factory(script))
    engine.start()
    return engine, store


def test_a_session_opens_with_its_conversation_id_and_what_it_can_do() -> None:
    engine, store = _engine(CodexScript())
    (opened,) = store.events_after(0)
    assert opened.offset == 1
    assert opened.opened.provider_conversation_id
    assert opened.opened.capabilities.steers
    assert opened.opened.worktree == "/work"
    assert engine.events_after(0) == [opened]


def test_a_turn_gives_items_deltas_usage_and_one_end() -> None:
    script = CodexScript().turn(
        Think("pondering"),
        RunCommand("ls", output="a\n"),
        Say("done"),
        Spend(input_tokens=10, output_tokens=2),
    )
    engine, store = _engine(script)
    engine.send(h.start_turn("e1", "t1"))
    assert engine.run_next() is True
    events = store.events_after(0)
    assert [e.offset for e in events] == list(range(1, len(events) + 1))
    kinds = h.kinds(events)
    assert kinds[0] == "opened"
    assert kinds[-1] == "turn_ended"
    assert kinds.count("turn_ended") == 1
    final = {e.item.id: e.item for e in events if e.HasField("item")}
    assert [i.WhichOneof("body") for i in final.values()] == ["reasoning", "tool", "assistant"]
    assert all(i.status == orch.ITEM_STATUS_FINISHED for i in final.values())
    assert all(i.turn_id == "t1" for i in final.values())
    ended = events[-1].turn_ended
    assert ended.status == orch.TURN_STATUS_COMPLETED
    assert ended.usage.input_tokens == 10
    assert ended.usage.output_tokens == 2
    deltas = [e.delta for e in events if e.HasField("delta")]
    assert deltas
    assert {d.item_id for d in deltas} <= set(final)


def test_a_command_the_provider_never_completes_is_finished_when_the_turn_completes() -> None:
    engine, store = _engine(CodexScript().turn(Leave("sleep 99"), Say("moving on")))
    engine.send(h.start_turn("e1", "t1"))
    engine.run_next()
    events = store.events_after(0)
    tool = [e.item for e in events if e.HasField("item") and e.item.WhichOneof("body") == "tool"]
    assert [t.status for t in tool] == [orch.ITEM_STATUS_RUNNING, orch.ITEM_STATUS_FINISHED]
    assert tool[0].id == tool[1].id
    assert h.kinds(events)[-1] == "turn_ended"


def test_the_same_effect_twice_runs_once() -> None:
    engine, store = _engine(CodexScript().turn(Say("one")).turn(Say("two")))
    command = h.start_turn("e1", "t1")
    engine.send(command)
    engine.send(command)
    assert engine.run_next() is True
    assert engine.run_next() is False
    assert h.kinds(store.events_after(0)).count("turn_ended") == 1


def test_a_command_for_another_session_is_refused() -> None:
    engine, _ = _engine(CodexScript())
    other = h.start_turn("e1", "t1")
    other.start_turn.session.session = 2
    with pytest.raises(InvalidCommandError):
        engine.send(other)


def test_a_command_that_breaks_its_contract_is_refused() -> None:
    engine, _ = _engine(CodexScript())
    with pytest.raises(InvalidCommandError):
        engine.send(h.start_turn("", "t1"))


def test_usage_reports_replace_one_another_instead_of_adding_up() -> None:
    script = CodexScript().turn(
        Spend(input_tokens=100, output_tokens=10), Say("a"), Spend(input_tokens=50, output_tokens=5)
    )
    engine, store = _engine(script)
    engine.send(h.start_turn("e1", "t1"))
    engine.run_next()
    events = store.events_after(0)
    reports = [e.usage.usage for e in events if e.HasField("usage")]
    assert reports
    assert events[-1].turn_ended.usage == reports[-1]


def test_a_steer_the_turn_came_too_late_for_is_answered_after_the_turn_ends() -> None:
    engine, store = _engine(CodexScript().turn(Say("quick")))
    engine.send(h.start_turn("e1", "t1"))
    engine.run_next()
    engine.send(h.steer("e2", "t1", "m-late"))
    tail = store.events_after(0)[-2:]
    assert h.kinds(tail) == ["turn_ended", "steer"]
    assert tail[1].steer.message_id == "m-late"
    assert tail[1].steer.accepted is False


class _Started:
    """A driver that sets *started* when the turn it runs produces its first event."""

    def __init__(self, inner: Driver, started: threading.Event) -> None:
        self._inner = inner
        self._started = started

    def run_turn(self, prompt: str, emit: Callable[[AgentEvent], None]) -> TurnResult:
        def emitting(event: AgentEvent) -> None:
            emit(event)
            self._started.set()

        return self._inner.run_turn(prompt, emitting)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def test_a_steer_sent_while_the_turn_runs_is_accepted_when_the_provider_takes_it() -> None:
    started = threading.Event()
    factory = h.codex_factory(CodexScript().turn(AwaitSteer(then=(Say("thanks"),))))
    store = MemoryStore()
    engine = Engine(h.launch(), store, lambda sink: _Started(factory(sink), started))  # type: ignore[arg-type,return-value]
    engine.start()
    engine.send(h.start_turn("e1", "t1"))
    # A steer comes from another thread while the turn thread reads the provider. The wait only
    # guards against a hang: the steer is sent as soon as the turn has produced its first event.
    sender = threading.Thread(
        target=lambda: (started.wait(30), engine.send(h.steer("e2", "t1", "m-steer")))
    )
    sender.start()
    engine.run_next()
    sender.join(30)
    assert not sender.is_alive()
    outcomes = [e.steer for e in store.events_after(0) if e.HasField("steer")]
    assert [(o.message_id, o.accepted) for o in outcomes] == [("m-steer", True)]
