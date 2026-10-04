"""Process-crash recovery over durable worker turns and genuinely pending Slurm jobs."""

from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import os
import threading
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

import pytest

from launch import LaunchSettings, create_session
from launch.testing import FakeStopTimer
from vibesys.api import Config, OrchestrationDescriptor, ResumeRef, RunRequest, RunResult
from vibesys.inputs import load_input_bundle
from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE
from vibesys.orchestration.dynamic.lifecycle import IntentKind, IntentStage
from vibesys.run.project_policy import build_project_path_policy
from vs_agent.api import AgentInvocationState, AgentSessionKey, Completed
from vs_project.api import Project
from vs_runtime.api.infrastructure import RunEnvironmentSpec
from vs_sandbox.api import create_compute_backend
from vs_slurm.fake_connector import SUBMITTED_FILE, active_jobs, pending_jobs, release_jobs

from ._harness import (
    PASS,
    LoopInput,
    ScriptedAgents,
    Turn,
    implemented,
    load_state,
    options,
    portfolio,
    workstream,
)

if TYPE_CHECKING:
    from multiprocessing.connection import Connection
    from pathlib import Path

    from vibesys.api import RunView
    from vs_agent.api import AgentInvocationStore
    from vs_runtime.api.infrastructure import RunState

_CRASH_EXIT = 70
type _Phase = Literal["pending", "settle-resume", "finish"]


def _request(loop_input: LoopInput, *, resume: bool) -> RunRequest:
    bundle = load_input_bundle(loop_input.root)
    configured = options()
    return RunRequest(
        run_id="pending-recovery",
        project_root=loop_input.root,
        orchestration=OrchestrationDescriptor(
            id=PLUGIN.id,
            config_version=PLUGIN.config_version,
            options=configured.model_dump(mode="json"),
        ),
        config=Config.model_validate({"model": {"name": "dynamic-loop"}}),
        input_bundle=bundle,
        objective=bundle.objective,
        skills_dirs=[str(path) for path in loop_input.skills_dirs],
        exp_name="pending-recovery",
        resume=ResumeRef(run_id="pending-recovery") if resume else None,
        agent_backend="cli",
        cli_provider="claude",
        profiler_kind=loop_input.profiler,
        backend=loop_input.backend,
        run_environment=RunEnvironmentSpec("slurm", {"config_path": str(loop_input.slurm_config)}),
    )


@dataclass
class _ObservedJournal:
    """Persist the real invocation model, then interrupt before host acknowledgment."""

    slot: AgentInvocationStore
    signal: Connection | None

    def load_optional(self) -> AgentInvocationState | None:
        return self.slot.load_optional()

    def save(self, model: AgentInvocationState) -> None:
        self.slot.save(model)
        resumed = [
            record.outcome
            for record in model.invocations.values()
            if isinstance(record.outcome, Completed)
            and record.outcome.invocation_id.endswith("/resume")
        ]
        if self.signal is not None and resumed:
            self.signal.send("resume-result-persisted")
            # A process crash cannot run scope cleanup; the parent reopens all
            # product resources from their durable public contracts.
            os._exit(_CRASH_EXIT)


def _journal_name(key: AgentSessionKey) -> str:
    return f"recovery-journal/{hashlib.sha256(str(key).encode()).hexdigest()}.json"


def _agents(
    loop_input: LoopInput, phase: _Phase, baseline_ready: threading.Event
) -> ScriptedAgents:
    agents = ScriptedAgents()
    if phase in {"pending", "settle-resume"}:
        agents.plan(portfolio(workstream("held")))

        def submit(agent: Turn) -> dict[str, object]:
            baseline_ready.wait()
            build_project_path_policy(loop_input.root, evaluator_source=None).resolve(
                agent.workspace
            )
            agent.set_value(2)
            if phase == "pending":
                loop_input.hold_jobs()
                announcement = loop_input.cluster / SUBMITTED_FILE
                os.mkfifo(announcement)
            handle = agent.submit("benchmark")
            if phase == "pending":
                announcement.read_text(encoding="utf-8")
                assert pending_jobs(loop_input.cluster, operation_id=handle)
                announcement.unlink()
            return {"kind": "waiting_for_evaluation", "handles": [handle]}

        agents.implement("held", submit)
        if phase == "settle-resume":
            agents.implement("held", implemented("held"))
    else:
        agents.implement("held", implemented("held"))
        agents.judge("held", PASS)

    return agents


def _execute(
    loop_input: LoopInput, phase: _Phase, signal: Connection | None = None
) -> tuple[RunResult, ScriptedAgents]:
    baseline_ready = threading.Event()
    agents = _agents(loop_input, phase, baseline_ready)

    def journal(state: RunState, key: AgentSessionKey) -> AgentInvocationStore:
        return _ObservedJournal(
            state.local("agent").slot(_journal_name(key), AgentInvocationState),
            signal
            if phase == "settle-resume"
            and key == AgentSessionKey.for_member(IMPLEMENTER.id, "held")
            else None,
        )

    async def run() -> RunResult:
        session = create_session(
            _request(loop_input, resume=phase not in {"pending", "settle-resume"}),
            sink=lambda _event: None,
            settings=LaunchSettings(
                agent_client_factory=lambda session_store, skill_selection, **_kwargs: (
                    agents.client(session_store=session_store, skill_selection=skill_selection)
                ),
                backend_factory=create_compute_backend,
                stop_timer=FakeStopTimer(),
                invocation_store_factory=journal,
            ),
        )

        def committed(view: RunView, _changed: tuple[str, ...] | None) -> None:
            state = load_state(loop_input, view.run_id)
            if state.baseline is not None:
                baseline_ready.set()
            if phase == "pending" and state.lifecycle.continuations:
                assert signal is not None
                signal.send("worker-suspension-persisted")
                os._exit(_CRASH_EXIT)

        session.on_committed_view(committed)
        if phase == "finish":
            session.on_ready(lambda _ready: release_jobs(loop_input.cluster))
        try:
            session.start()
            return await session.await_result()
        finally:
            session.close()

    return asyncio.run(run()), agents


def _crash(loop_input: LoopInput, phase: _Phase) -> None:
    context = multiprocessing.get_context("fork")
    received, sent = context.Pipe(duplex=False)
    child = context.Process(target=_execute, args=(loop_input, phase, sent))
    child.start()
    sent.close()
    try:
        assert received.recv() == (
            "worker-suspension-persisted" if phase == "pending" else "resume-result-persisted"
        )
        child.join()
        assert child.exitcode == _CRASH_EXIT
    finally:
        received.close()
        if child.is_alive():
            child.kill()
            child.join()
        child.close()


def _input(tmp_path: Path) -> LoopInput:
    loop_input = LoopInput.create(tmp_path)
    skill = tmp_path / "resources" / "skills" / "recovery-policy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: recovery-policy\ndescription: Preserve accuracy.\n---\n# Recovery policy\n",
        encoding="utf-8",
    )
    (skill / "floor.md").write_text("Preserve accuracy.\n", encoding="utf-8")
    (loop_input.root / "OBJECTIVE.md").write_text(
        "Raise queue throughput. Follow resources/skills/recovery-policy/floor.md.\n",
        encoding="utf-8",
    )
    return replace(loop_input, skills_dirs=(skill,))


@pytest.mark.parametrize("result_before_ack", [False, True])
def test_pending_worker_recovery_resumes_once_without_replaying_accepted_work(
    tmp_path: Path, *, result_before_ack: bool
) -> None:
    loop_input = _input(tmp_path)
    _crash(loop_input, "settle-resume" if result_before_ack else "pending")
    waiting = load_state(loop_input, "pending-recovery")
    assert waiting.baseline is not None
    assert waiting.baseline.benchmark_passed
    assert waiting.baseline.metric_value == 1.0
    (continuation,) = waiting.lifecycle.continuations.values()
    if not result_before_ack:
        assert pending_jobs(loop_input.cluster, operation_id=continuation.dependencies[0].handle)
        assert continuation.settlements == {}
    key = AgentSessionKey.parse(continuation.session_key)
    assert key == AgentSessionKey.for_member(IMPLEMENTER.id, "held")
    journal_slot = (
        Project.open(loop_input.root)
        .state.local_namespace("pending-recovery", "agent")
        .slot(_journal_name(key), AgentInvocationState)
    )
    initial_journal = journal_slot.load_optional()
    assert initial_journal is not None
    yielded = initial_journal.invocations[continuation.yielded_invocation_id].outcome
    assert isinstance(yielded, Completed)
    commands_at_crash = loop_input.cluster_commands()
    assert not any(command.startswith("scancel ") for command in commands_at_crash)

    if result_before_ack:
        accepted = waiting
        assert any(
            intent.kind is IntentKind.RESUME and intent.stage is IntentStage.DISPATCHED
            for intent in accepted.lifecycle.intents.values()
        )
        assert accepted.workstreams[0].implementation is None
    result, agents = _execute(loop_input, "finish")

    assert result.succeeded is True
    assert agents.unscripted == []
    final = load_state(loop_input, "pending-recovery")
    (member,) = final.workstreams
    assert member.last_error is None, member.last_error
    assert member.budget == waiting.workstreams[0].budget
    assert member.candidate_revision == continuation.retained_revision
    assert member.evaluation is not None
    assert member.evaluation.metric_value == 2.0
    assert member.evaluation.accuracy_passed
    assert member.evaluation.benchmark_passed
    assert final.winner_revision == member.candidate_revision
    assert len(agents.prompts(IMPLEMENTER.id)) == (0 if result_before_ack else 1)
    assert len(agents.prompts(JUDGE.id)) == 1
    final_journal = journal_slot.load_optional()
    assert final_journal is not None
    resumed = final_journal.invocations[f"{continuation.continuation_id}/resume"].outcome
    assert isinstance(resumed, Completed)
    assert resumed.result.provider_session_id == yielded.result.provider_session_id
    assert active_jobs(loop_input.cluster) == ()
    assert sum("sbatch " in command for command in loop_input.cluster_commands()) == 3
    project = Project.open(loop_input.root)
    runtime = project.state.portable_namespace("pending-recovery", "runtime")
    assert "Follow .agents/skills/recovery-policy/floor.md" in (
        runtime.external_directory() / "effective-objective.md"
    ).read_text(encoding="utf-8")
