"""A stopped dynamic run starts no new work and ends within its grace bound."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from tests.vibesys.orchestration.dynamic.loop._harness import (
    PASS,
    AgentTransportError,
    LoopInput,
    ScriptedAgents,
    Turn,
    implemented,
    load_state,
    options,
    portfolio,
    profile_workstream,
    run_loop,
    workstream,
)

from vibesys.api import RunStatus
from vibesys.api.testing import FakeStopTimer
from vibesys.orchestration.dynamic.agents import ORCHESTRATOR, PROFILER
from vibesys.run.host import STOP_GRACE_S

if TYPE_CHECKING:
    from pathlib import Path

# Deadlock guards: each wait ends within seconds when the code is correct,
# and raising them never turns a failure into a pass.
_GUARD_S = 120.0


def _sbatch_after(commands: list[str], index: int) -> list[str]:
    return [command for command in commands[index:] if "sbatch" in command]


def test_a_stop_mid_turn_rejects_new_work_cancels_the_job_and_ends_at_the_grace_bound(
    tmp_path: Path,
) -> None:
    loop_input = LoopInput.create(tmp_path)
    os.mkfifo(loop_input.submitted)
    # The input measurement and the implementer's evaluation stay queued.
    loop_input.hold_jobs()
    timer = FakeStopTimer()
    sessions: list[Any] = []
    jobs: list[str] = []
    seen: dict[str, object] = {}

    def plan_once_the_input_job_is_queued(_agent: Turn) -> dict[str, object]:
        jobs.append(loop_input.submitted.read_text(encoding="utf-8"))
        return portfolio(workstream("H1"))

    def stop_while_evaluating(agent: Turn) -> dict[str, object]:
        agent.set_value(2)
        agent.submit("accuracy", "benchmark")
        jobs.append(loop_input.submitted.read_text(encoding="utf-8"))
        # Later submissions, if any reach the cluster, must not block on the FIFO.
        loop_input.submitted.unlink()
        seen["commands_at_stop"] = len(loop_input.cluster_commands())
        (session,) = sessions
        session.stop()
        seen["armed"] = timer.wait_armed(_GUARD_S)
        seen["commands_in_grace"] = loop_input.cluster_commands()
        seen["resubmit"] = agent.submit_reply("accuracy", "benchmark")
        # The turn keeps running past the grace period; only a cancel ends it.
        timer.expire()
        seen["cancelled"] = agents.wait_cancelled(_GUARD_S)
        message = "agent process killed"
        raise AgentTransportError(message)

    agents = (
        ScriptedAgents()
        .plan(plan_once_the_input_job_is_queued)
        .implement("H1", stop_while_evaluating)
        .judge("H1", PASS)
    )

    run = run_loop(
        loop_input,
        agents,
        options(max_rounds=2, max_retries_per_round=2),
        on_session=sessions.append,
        stop_timer=timer,
    )

    assert run.error is None, run.error
    assert run.status is RunStatus.STOPPED
    assert agents.unscripted == []
    assert seen["armed"] is True
    assert seen["cancelled"] is True
    resubmit = seen["resubmit"]
    assert isinstance(resubmit, dict)
    assert resubmit["kind"] == "run_stopping"
    assert "handle_id" not in resubmit
    input_job, evaluation_job = jobs
    # The running evaluation is cancelled at the stop, not at the grace bound.
    commands_in_grace = seen["commands_in_grace"]
    assert isinstance(commands_in_grace, list)
    assert f"scancel {evaluation_job}" in commands_in_grace
    commands = loop_input.cluster_commands()
    commands_at_stop = seen["commands_at_stop"]
    assert isinstance(commands_at_stop, int)
    assert _sbatch_after(commands, commands_at_stop) == []
    assert f"scancel {input_job}" in commands
    # One grace period on the injected clock bounded the run.
    assert timer.delays == [STOP_GRACE_S]
    assert len(agents.prompts(ORCHESTRATOR.id)) == 1


def test_a_turn_that_ends_after_a_stop_starts_no_planner_turn_or_evaluation(
    tmp_path: Path,
) -> None:
    loop_input = LoopInput.create(tmp_path)
    os.mkfifo(loop_input.submitted)
    # The input measurement stays queued, so it is running at the stop.
    loop_input.hold_jobs()
    timer = FakeStopTimer()
    sessions: list[Any] = []
    jobs: list[str] = []
    seen: dict[str, object] = {}

    def plan_once_the_input_job_is_queued(_agent: Turn) -> dict[str, object]:
        jobs.append(loop_input.submitted.read_text(encoding="utf-8"))
        loop_input.submitted.unlink()
        return portfolio(workstream("H1"))

    def stop_then_finish(agent: Turn) -> dict[str, object]:
        agent.set_value(2)
        seen["commands_at_stop"] = len(loop_input.cluster_commands())
        (session,) = sessions
        session.stop()
        seen["resubmit"] = agent.submit_reply("accuracy", "benchmark")
        return implemented("H1")

    agents = (
        ScriptedAgents()
        .plan(plan_once_the_input_job_is_queued, portfolio(workstream("H2")))
        .implement("H1", stop_then_finish)
        .implement("H2", implemented("H2"))
        .judge("H1", PASS)
        .judge("H2", PASS)
    )

    run = run_loop(
        loop_input,
        agents,
        options(max_rounds=2),
        on_session=sessions.append,
        stop_timer=timer,
    )

    assert run.error is None, run.error
    assert run.status is RunStatus.STOPPED
    resubmit = seen["resubmit"]
    assert isinstance(resubmit, dict)
    assert resubmit["kind"] == "run_stopping"
    assert len(agents.prompts(ORCHESTRATOR.id)) == 1
    commands_at_stop = seen["commands_at_stop"]
    assert isinstance(commands_at_stop, int)
    commands = loop_input.cluster_commands()
    assert _sbatch_after(commands, commands_at_stop) == []
    (input_job,) = jobs
    assert f"scancel {input_job}" in commands
    assert agents.unscripted == []


_UNSUPPORTED: dict[str, object] = {
    "outcome": "unsupported",
    "narrative": "No profiler reaches this candidate.",
    "unsupported_reason": "the profiler cannot attach to this process",
}


def test_a_profile_a_stop_interrupts_has_no_outcome_and_runs_again_on_resume(
    tmp_path: Path,
) -> None:
    """Chaos seed 4025: the interrupted profile was committed failed and never rerun."""
    loop_input = LoopInput.create(tmp_path, profiled=True)
    sessions: list[Any] = []

    def stop_during_the_profile(_agent: Turn) -> dict[str, object]:
        (session,) = sessions
        session.stop()
        # A reply the profiler must correct: its correction turn is the next
        # turn boundary, where the stop lands.
        return {"outcome": "observed"}

    first = (
        ScriptedAgents()
        .plan(portfolio(profile_workstream("prof-1", None)))
        .profile(stop_during_the_profile)
    )
    stopped = run_loop(
        loop_input,
        first,
        options(max_rounds=2),
        on_session=sessions.append,
        stop_timer=FakeStopTimer(),
    )

    assert stopped.error is None, stopped.error
    assert stopped.status is RunStatus.STOPPED
    (interrupted,) = load_state(loop_input, stopped.run_id).profiles
    assert interrupted.outcome is None

    second = (
        ScriptedAgents()
        .plan(portfolio(workstream("H1")), portfolio(workstream("H2")))
        .profile(_UNSUPPORTED)
        .implement("H1", implemented("H1"))
        .implement("H2", implemented("H2"))
        .judge("H1", PASS)
        .judge("H2", PASS)
    )
    resumed = run_loop(loop_input, second, options(max_rounds=2), resume_run_id=stopped.run_id)

    assert resumed.error is None, resumed.error
    (rerun,) = load_state(loop_input, resumed.run_id).profiles
    assert rerun.outcome is not None
    assert rerun.outcome.status.value == "unsupported"
    assert len(second.prompts(PROFILER.id)) == 1
