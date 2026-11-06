"""Activity between turns: one fold per idle stretch, no usage, settled when the stretch is over."""

from __future__ import annotations

from agentshim import AssistantText, ProviderUsage, TokenUsage, ToolCall, ToolResult, UsageReport

from tests import helpers as h
from tests.fakes import ScriptedDriver, TurnScript
from vs_sidecar.engine import Engine
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.store import MemoryStore


def _engine(driver: ScriptedDriver) -> tuple[Engine, MemoryStore]:
    store = MemoryStore()
    engine = Engine(h.REF, store, lambda _sink: driver)
    engine.start()
    return engine, store


def _items(store: MemoryStore) -> list[tuple[str, int, str]]:
    return [
        (e.item.id, e.item.status, e.item.tool.shell.command)
        for e in store.events_after(0)
        if e.HasField("item")
    ]


def test_a_tool_call_that_spans_two_polls_keeps_its_arguments_when_it_finishes() -> None:
    driver = ScriptedDriver(
        idle=[
            [ToolCall("c1", "Bash", {"command": "ls"})],
            [ToolResult("c1", "Bash", "out", "", 0, 0.0)],
        ]
    )
    engine, store = _engine(driver)
    engine.poll_idle()
    engine.poll_idle()
    assert _items(store) == [
        ("c1", orch.ITEM_STATUS_RUNNING, "ls"),
        ("c1", orch.ITEM_STATUS_FINISHED, "ls"),
    ]


def test_idle_ids_do_not_collide_with_the_ids_a_turn_mints_and_each_stretch_has_its_own() -> None:
    driver = ScriptedDriver(
        [TurnScript([AssistantText("in turn", None)])],
        idle=[[AssistantText("idle one", None)], [AssistantText("idle two", None)]],
    )
    engine, store = _engine(driver)
    engine.poll_idle()
    engine.send(h.start_turn("e1", "t1"))
    engine.run_next()
    engine.poll_idle()
    ids = [i for i, _, _ in _items(store)]
    assert len(ids) == len(set(ids)) == 3


def test_a_tool_call_still_running_when_the_idle_stretch_ends_is_settled_failed() -> None:
    driver = ScriptedDriver([TurnScript([])], idle=[[ToolCall("c1", "Bash", {"command": "sleep"})]])
    engine, store = _engine(driver)
    engine.poll_idle()
    engine.send(h.start_turn("e1", "t1"))
    engine.run_next()
    statuses = [status for _, status, _ in _items(store)]
    assert statuses == [orch.ITEM_STATUS_RUNNING, orch.ITEM_STATUS_FAILED]
    kinds = h.kinds(store.events_after(0))
    assert kinds.index("turn_ended") > len(statuses)


def test_idle_activity_reports_no_usage_and_never_touches_an_ended_turns_usage() -> None:
    usage = ProviderUsage(tokens=TokenUsage(input_tokens=5, output_tokens=1))
    driver = ScriptedDriver([TurnScript([], usage=usage)], idle=[[UsageReport(usage, None)]])
    engine, store = _engine(driver)
    engine.send(h.start_turn("e1", "t1"))
    engine.run_next()
    engine.poll_idle()
    reports = [e.usage for e in store.events_after(0) if e.HasField("usage")]
    assert [r.turn_id for r in reports] == ["t1"]
