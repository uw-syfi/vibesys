"""``AgentshimDriver`` sends an interrupt or a steer to the turn it names and to no other."""

from __future__ import annotations

import pytest
from agentshim.testing import CodexScript, Say

from tests import helpers as h
from vs_sidecar.driver import Driver, SteerRefusedError
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch


def _driver(script: CodexScript) -> Driver:
    driver = h.codex_factory(script)(lambda _frame: None)
    driver.open()
    return driver


def _kinds(driver: Driver, turn_id: str) -> tuple[int, list[str]]:
    seen: list[str] = []
    driver.begin_turn(turn_id, "go")
    result = driver.run_turn(lambda event: seen.append(type(event).__name__))
    return result.status, seen


def test_an_interrupt_after_the_turn_began_and_before_it_ran_cancels_it_without_asking_the_provider() -> (
    None
):
    driver = _driver(CodexScript().turn(Say("never")))
    driver.begin_turn("t1", "go")
    driver.interrupt("t1")
    seen: list[str] = []
    result = driver.run_turn(lambda event: seen.append(type(event).__name__))
    assert result.status == orch.TURN_STATUS_INTERRUPTED
    assert "AssistantText" not in seen


def test_an_interrupt_for_a_turn_that_ended_leaves_the_next_turn_alone() -> None:
    driver = _driver(CodexScript().turn(Say("one")).turn(Say("two")))
    assert _kinds(driver, "t1")[0] == orch.TURN_STATUS_COMPLETED
    driver.begin_turn("t2", "go")
    driver.interrupt("t1")
    result = driver.run_turn(lambda _event: None)
    assert result.status == orch.TURN_STATUS_COMPLETED


def test_an_interrupt_with_no_turn_begun_does_nothing_and_does_not_wait_for_the_next() -> None:
    driver = _driver(CodexScript().turn(Say("one")))
    driver.interrupt("t1")
    assert _kinds(driver, "t1")[0] == orch.TURN_STATUS_COMPLETED


def test_a_steer_for_a_turn_that_is_not_the_current_one_is_refused() -> None:
    driver = _driver(CodexScript().turn(Say("one")).turn(Say("two")))
    _kinds(driver, "t1")
    with pytest.raises(SteerRefusedError):
        driver.steer("t1", "late")
    driver.begin_turn("t2", "go")
    with pytest.raises(SteerRefusedError):
        driver.steer("t1", "late")
    with pytest.raises(SteerRefusedError):
        driver.steer("t2", "before it is running")
