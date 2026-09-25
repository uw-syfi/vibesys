"""Shared crash-and-resume harness for the ``multi`` and ``single`` strategies.

``tests/vibesys/golden/harness.py::run_scripted`` drives one strategy
end-to-end against a scripted :class:`~vs_agent.api.testing.FakeAgentClient`
but always starts a fresh project. This module adds the one thing golden
does not need: resuming a run against the *same* on-disk project across two
scripted calls, following the pattern
``tests/vibesys/loops/issue_queue/_support.py`` established for the ``plain``
strategy. It is shared by ``tests/vibesys/loops/multi/`` and
``tests/vibesys/loops/single/`` since both strategies share the same
``RunRequest``/``run_orchestration`` construction shape.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest
from tests.vibesys.golden.harness import (
    _fake_backend_factory,
    _SharedFakeClient,
    write_minimal_input_bundle,
)

from vibesys.config import Config, as_config
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.orchestration.request import ResumeRef, RunRequest
from vibesys.orchestration.runner import run_orchestration
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import Project

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.backends.base import ComputeBackendImpl
    from vibesys.orchestration.contracts import Orchestrator
    from vibesys.orchestration.gates import GateExecutor
    from vs_agent.api import AgentClientProtocol
    from vs_agent.api.testing import FakeAgentClient
    from vs_project.api import OrchestrationDescriptor


@dataclass(frozen=True, slots=True)
class AgentRun:
    """A completed (or resumed) scripted multi/single run."""

    result: bool
    project_dir: Path
    run_id: str


def _build_request(
    tmp_path: Path,
    descriptor: OrchestrationDescriptor,
    config: Config,
    *,
    exp_name: str,
    resume_from: AgentRun | None,
) -> RunRequest:
    if resume_from is None:
        input_dir = write_minimal_input_bundle(tmp_path)
        bundle = load_input_bundle(input_dir)
        return RunRequest(
            project_root=bundle.root,
            orchestration=descriptor,
            config=config,
            input_bundle=bundle,
            objective=bundle.objective,
            exp_name=exp_name,
            runs_dir=tmp_path / "exp_env",
        )
    bundle = load_input_bundle(resume_from.project_dir)
    return RunRequest(
        project_root=bundle.root,
        orchestration=descriptor,
        config=config,
        input_bundle=bundle,
        objective=bundle.objective,
        exp_name=resume_from.project_dir.name,
        runs_dir=tmp_path / "exp_env",
        resume=ResumeRef(run_id=resume_from.run_id),
    )


async def _execute(  # noqa: PLR0913  # tracked: #288
    request: RunRequest,
    orchestrator_factory: type[Orchestrator],
    descriptor: OrchestrationDescriptor,
    runner: object,
    *,
    backend_factory: Callable[..., ComputeBackendImpl] | None,
    gate_executor: GateExecutor | None,
) -> bool:
    integration = LocalRunIntegration()
    try:
        return await run_orchestration(
            request,
            integration,
            orchestrator_factory(descriptor),
            agent_client_factory=cast(
                "Callable[..., AgentClientProtocol]",
                lambda **_kwargs: _SharedFakeClient(cast("FakeAgentClient", runner)),
            ),
            backend_factory=backend_factory or _fake_backend_factory,
            gate_executor=gate_executor,
        )
    finally:
        integration.close()


def run_agent_loop(  # noqa: PLR0913  # tracked: #288
    tmp_path: Path,
    runner: FakeAgentClient,
    orchestrator_factory: type[Orchestrator],
    descriptor: OrchestrationDescriptor,
    *,
    exp_name: str = "agent-test",
    resume_from: AgentRun | None = None,
    backend_factory: Callable[..., ComputeBackendImpl] | None = None,
    gate_executor: GateExecutor | None = None,
) -> AgentRun:
    """Run (or resume) a multi/single orchestrator once with a scripted client.

    A fresh call (``resume_from=None``) provisions a new project under
    ``tmp_path / "exp_env"``. Pass a prior call's ``AgentRun`` as
    ``resume_from`` to resume that same on-disk project, exercising
    resume-after-crash behavior. ``backend_factory``/``gate_executor``
    default to the golden harness's ``FakeComputeBackend`` seam and the real
    gate executor; pass either to script a compute-backend-owned command
    (e.g. profile-guided attribution) or the accuracy/benchmark gates
    without ``unittest.mock.patch``.
    """
    config = as_config(Config.model_validate({"model": {"name": "claude-golden-test"}}))
    request = _build_request(
        tmp_path, descriptor, config, exp_name=exp_name, resume_from=resume_from
    )

    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        result = asyncio.run(
            _execute(
                request,
                orchestrator_factory,
                descriptor,
                runner,
                backend_factory=backend_factory,
                gate_executor=gate_executor,
            )
        )

    project_dir = resume_from.project_dir if resume_from else _sole_project_dir(tmp_path)
    run_id = _sole_run_id(project_dir)
    return AgentRun(result=result, project_dir=project_dir, run_id=run_id)


def run_agent_loop_expect_crash(  # noqa: PLR0913  # tracked: #288
    tmp_path: Path,
    runner: FakeAgentClient,
    orchestrator_factory: type[Orchestrator],
    descriptor: OrchestrationDescriptor,
    error: type[BaseException],
    *,
    exp_name: str = "agent-test",
) -> AgentRun:
    """Run once, expecting ``error`` mid-flight (via ``runner.fail(...)``).

    Simulates a killed process without ``unittest.mock.patch``: the crash is
    injected through ``FakeAgentClient.fail``'s real seam, exactly like a
    real agent-turn failure would surface. Returns an ``AgentRun`` (``result``
    is meaningless) pointing at the project/run the crash left on disk, for a
    follow-up ``run_agent_loop(..., resume_from=...)`` call.
    """
    config = as_config(Config.model_validate({"model": {"name": "claude-golden-test"}}))
    request = _build_request(tmp_path, descriptor, config, exp_name=exp_name, resume_from=None)

    with (
        patch("vibesys.context.PROJECT_ROOT", tmp_path),
        pytest.raises(error),
    ):
        asyncio.run(
            _execute(
                request,
                orchestrator_factory,
                descriptor,
                runner,
                backend_factory=None,
                gate_executor=None,
            )
        )

    project_dir = _sole_project_dir(tmp_path)
    run_id = _sole_run_id(project_dir)
    return AgentRun(result=False, project_dir=project_dir, run_id=run_id)


def _sole_project_dir(tmp_path: Path) -> Path:
    projects = [p for p in (tmp_path / "exp_env").iterdir() if p.is_dir()]
    assert len(projects) == 1, f"expected exactly one project directory, found {projects}"
    return projects[0]


def _sole_run_id(project_dir: Path) -> str:
    runs = Project.open(project_dir).state.list_runs()
    assert len(runs) == 1, runs
    return runs[0].run_id
