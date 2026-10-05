"""A stopped dynamic run starts no new work and ends within its grace bound, on the core path."""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

import pytest
from tests.composition.dynamic._harness import (
    PASS,
    AgentTransportError,
    LoopInput,
    ScriptedAgents,
    Turn,
    implemented,
    portfolio,
    run_request,
    workstream,
)

from vibesys.api import RunStatus
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.run.host import STOP_GRACE_S
from vs_runtime.api.testing import FakeStopTimer

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api import RunHandle

# Deadlock guards: each wait ends within seconds when the code is correct, and
# raising them never turns a failure into a pass.
_GUARD_S = 120.0
_STUCK_TURN_GUARD_S = 15.0


class _Handles:
    """The run's handle, captured when the run starts."""

    def __init__(self) -> None:
        self.items: list[RunHandle] = []

    def __call__(self, handle: RunHandle) -> None:
        self.items.append(handle)

    def stop(self) -> None:
        (handle,) = self.items
        handle.stop()


def _stop_when_a_job_is_pending(
    loop_input: LoopInput, handles: _Handles, timer: FakeStopTimer
) -> list[str]:
    """Stop the run once the cluster announces a held job, then let its grace period pass.

    Returns the job ids seen. The held job never finishes by itself, so only the grace
    bound (the injected timer) and the cancel that follows it can end the run.
    """
    os.mkfifo(loop_input.submitted)
    jobs: list[str] = []

    def watch() -> None:
        jobs.append(loop_input.submitted.read_text(encoding="utf-8"))
        handles.stop()
        if timer.wait_armed(_GUARD_S):
            timer.expire()

    threading.Thread(target=watch, daemon=True).start()
    return jobs


def _sbatch_after(commands: list[str], index: int) -> list[str]:
    return [command for command in commands[index:] if "sbatch" in command]


def test_a_stop_during_the_input_measurement_cancels_its_job_and_starts_no_planner_turn(
    tmp_path: Path,
) -> None:
    loop_input = LoopInput.create(tmp_path)
    # The input measurement stays queued until the run cancels it.
    loop_input.hold_jobs()
    handles = _Handles()
    timer = FakeStopTimer()
    jobs = _stop_when_a_job_is_pending(loop_input, handles, timer)
    agents = ScriptedAgents().plan(portfolio(workstream("H1")))

    run = run_request(
        loop_input.request(max_rounds=2),
        agents,
        on_handle=handles,
        stop_timer=timer,
    )

    assert run.error is None, run.error
    assert run.status is RunStatus.STOPPED
    assert timer.delays == [STOP_GRACE_S]
    (job,) = jobs
    commands = loop_input.cluster_commands()
    assert f"scancel {job}" in commands
    assert loop_input.sbatch_count() == 1
    assert agents.prompts(ORCHESTRATOR.id) == []
    assert agents.unscripted == []


def test_a_stop_during_a_candidate_evaluation_cancels_its_job_and_starts_no_new_work(
    tmp_path: Path,
) -> None:
    loop_input = LoopInput.create(tmp_path)
    handles = _Handles()
    timer = FakeStopTimer()
    seen: dict[str, object] = {}

    def nominate_then_hold_the_evaluation(agent: Turn) -> dict[str, object]:
        agent.set_value(2)
        # The baseline already ran. The candidate's evaluation job now stays queued.
        loop_input.hold_jobs()
        _stop_when_a_job_is_pending(loop_input, handles, timer)
        seen["jobs_before_nomination"] = loop_input.sbatch_count()
        return implemented("H1")

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("H1")), portfolio(workstream("H2")))
        .implement("H1", nominate_then_hold_the_evaluation)
        .judge("H1", PASS)
    )

    run = run_request(
        loop_input.request(max_rounds=2),
        agents,
        on_handle=handles,
        stop_timer=timer,
    )

    assert run.error is None, run.error
    assert run.status is RunStatus.STOPPED
    assert timer.delays == [STOP_GRACE_S]
    assert seen["jobs_before_nomination"] == 1
    # The only job submitted after the nomination is the held evaluation, and it is cancelled.
    commands = loop_input.cluster_commands()
    assert loop_input.sbatch_count() == 2
    assert agents.unscripted == []
    assert len(agents.prompts(ORCHESTRATOR.id)) == 1
    # The review ran before the evaluation; nothing new starts after the stop.
    assert len(agents.prompts(JUDGE.id)) == 1
    assert len(agents.prompts(IMPLEMENTER.id)) == 1
    assert any(command.startswith("scancel ") for command in commands)


_CANCEL_GAP = (
    "gap (owner: vs-runtime): when the grace bound cancels the run, a core agent turn "
    "still running is waited out and the provider turn is never cancelled "
    "(libs/vs-runtime/src/vs_runtime/_agent_sessions.py:99 await_session_operation "
    "swallows the cancel, and _session_requests.py:661 hands it no client to cancel), so "
    "a stuck provider turn outlives the grace bound. The legacy path cancelled the client."
)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_CANCEL_GAP)
def test_a_stop_mid_turn_ends_at_the_grace_bound_and_starts_no_evaluation(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    handles = _Handles()
    timer = FakeStopTimer()
    seen: dict[str, object] = {}

    def stop_then_hang(agent: Turn) -> dict[str, object]:
        agent.set_value(2)
        seen["commands_at_stop"] = len(loop_input.cluster_commands())
        handles.stop()
        seen["armed"] = timer.wait_armed(_GUARD_S)
        # The turn keeps running past the grace period; only a cancel ends it.
        timer.expire()
        seen["cancelled"] = agents.wait_cancelled(_STUCK_TURN_GUARD_S)
        message = "agent process killed"
        raise AgentTransportError(message)

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("H1")), portfolio(workstream("H2")))
        .implement("H1", stop_then_hang)
        .judge("H1", PASS)
    )

    run = run_request(
        loop_input.request(max_rounds=2),
        agents,
        on_handle=handles,
        stop_timer=timer,
    )

    assert run.error is None, run.error
    assert run.status is RunStatus.STOPPED
    assert seen["armed"] is True
    assert seen["cancelled"] is True
    # One grace period on the injected clock bounded the run.
    assert timer.delays == [STOP_GRACE_S]
    commands_at_stop = seen["commands_at_stop"]
    assert isinstance(commands_at_stop, int)
    assert _sbatch_after(loop_input.cluster_commands(), commands_at_stop) == []
    assert len(agents.prompts(ORCHESTRATOR.id)) == 1
    assert agents.prompts(JUDGE.id) == []
    assert agents.unscripted == []


def test_a_turn_that_ends_after_a_stop_starts_no_planner_turn_or_evaluation(
    tmp_path: Path,
) -> None:
    loop_input = LoopInput.create(tmp_path)
    handles = _Handles()
    seen: dict[str, object] = {}

    def stop_then_finish(agent: Turn) -> dict[str, object]:
        agent.set_value(2)
        seen["commands_at_stop"] = len(loop_input.cluster_commands())
        handles.stop()
        return implemented("H1")

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("H1")), portfolio(workstream("H2")))
        .implement("H1", stop_then_finish)
        .implement("H2", implemented("H2"))
        .judge("H1", PASS)
        .judge("H2", PASS)
    )

    run = run_request(
        loop_input.request(max_rounds=2),
        agents,
        on_handle=handles,
        stop_timer=FakeStopTimer(),
    )

    assert run.error is None, run.error
    assert run.status is RunStatus.STOPPED
    commands_at_stop = seen["commands_at_stop"]
    assert isinstance(commands_at_stop, int)
    assert _sbatch_after(loop_input.cluster_commands(), commands_at_stop) == []
    assert len(agents.prompts(ORCHESTRATOR.id)) == 1
    assert agents.prompts(IMPLEMENTER.id, "H2") == []
    assert agents.unscripted == []
