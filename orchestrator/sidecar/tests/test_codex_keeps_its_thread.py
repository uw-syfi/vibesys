"""A Codex session keeps one thread for its whole life, however its app-server process comes and goes.

The lead of a run is told its brief once, in its first turn. If a later turn started an empty
thread, the lead would forget the brief. These tests drive the real driver over agentshim's
scripted app-server, which keeps its threads across processes, and kill the process at every
point a process can go away.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshim.testing import CodexScript, Crash, FakeExecutor, Say
from hypothesis import given, settings
from hypothesis import strategies as st

from tests import helpers as h
from vs_sidecar.driver import AgentshimDriver
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.main import build_session

if TYPE_CHECKING:
    from agentshim import AgentEvent

    from vs_sidecar.driver import Driver, TurnResult

IDLE = None
BETWEEN_TURNS = "the process exits between turns"
MID_TURN = "the process crashes mid-turn"


def _driver(script: CodexScript) -> tuple[Driver, FakeExecutor]:
    executor = FakeExecutor([], peers=script.peer)
    agent = h.scripted_agents("codex", executor)
    driver = AgentshimDriver(
        lambda conversation_id: build_session(
            h.launch(),
            agent=agent,
            raw_sink=lambda _frame: None,
            new_conversation_id=conversation_id,
        )
    )
    return driver, executor


def _turn(driver: Driver, number: int) -> TurnResult:
    driver.begin_turn(f"t{number}", f"turn {number}")
    events: list[AgentEvent] = []
    return driver.run_turn(events.append)


def _starts(script: CodexScript) -> int:
    return len(script.requests("thread/start"))


@settings(max_examples=60, deadline=None)
@given(exits=st.lists(st.sampled_from([IDLE, BETWEEN_TURNS, MID_TURN]), min_size=1, max_size=6))
def test_every_turn_runs_in_the_thread_the_session_opened(exits: list[str | None]) -> None:
    script = CodexScript()
    for exit_ in exits:
        script.turn(*((Say("working"), Crash(9, "boom")) if exit_ == MID_TURN else (Say("ok"),)))
    driver, executor = _driver(script)
    thread_id = driver.open().conversation_id
    assert thread_id
    for number, exit_ in enumerate(exits):
        if exit_ == BETWEEN_TURNS:
            executor.processes[-1].kill()
        result = _turn(driver, number)
        expected = orch.TURN_STATUS_FAILED if exit_ == MID_TURN else orch.TURN_STATUS_COMPLETED
        assert result.status == expected
    assert _starts(script) == 1
    assert script.thread_ids() == [thread_id]
    driver.close()


def test_a_session_whose_process_exited_before_its_first_turn_resumes_its_thread() -> None:
    script = CodexScript().turn(Say("ok"))
    driver, executor = _driver(script)
    thread_id = driver.open().conversation_id
    executor.processes[0].kill()
    assert _turn(driver, 0).status == orch.TURN_STATUS_COMPLETED
    assert _starts(script) == 1
    assert len(script.requests("thread/resume")) == 1
    assert script.thread_ids() == [thread_id]
    driver.close()


def test_the_third_turn_continues_the_thread_of_the_first_two() -> None:
    # Agentshim retires a Codex thread after two turns unless the caller pins it; the lead
    # of a run must not lose its brief that way.
    script = CodexScript().turn(Say("one")).turn(Say("two")).turn(Say("three"))
    driver, _ = _driver(script)
    driver.open()
    for number in range(3):
        assert _turn(driver, number).status == orch.TURN_STATUS_COMPLETED
    assert _starts(script) == 1
    assert script.spawned == 1
    driver.close()


def test_a_thread_codex_no_longer_has_fails_the_turn_by_name_and_starts_no_empty_thread() -> None:
    script = CodexScript().turn(Say("one"))
    driver, executor = _driver(script)
    thread_id = driver.open().conversation_id
    assert thread_id
    assert _turn(driver, 0).status == orch.TURN_STATUS_COMPLETED
    script.forget(thread_id)
    executor.processes[-1].kill()
    result = _turn(driver, 1)
    assert result.status == orch.TURN_STATUS_FAILED
    assert "SessionResumeError" in (result.detail or "")
    assert thread_id in (result.detail or "")
    assert _starts(script) == 1
    assert script.thread_ids() == []
    driver.close()
