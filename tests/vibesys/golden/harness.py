"""Drive one real strategy ``run(ctx)`` end-to-end against a scripted
:class:`~vs_agent.api.testing.FakeAgentClient`, the same pattern
``tests/vibesys/loops/evolve/test_evolutionary_loop.py`` and
``tests/vibesys/loops/issue_queue/test_plain_loop.py`` already use for their
own strategies. This module generalizes that pattern across every registered
strategy so golden tests can capture exactly what a scripted round writes
and emits, without a real agent CLI or sandbox.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from unittest.mock import patch  # test-isolation: seams scripted below

from vibesys.api.testing import FakeComputeBackend
from vibesys.config import Config, as_config
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.orchestration.request import RunRequest
from vibesys.orchestration.runner import run_orchestration
from vibesys.profilers import ProfilerKind
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import Project

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.backends.base import ComputeBackendImpl
    from vibesys.constants import ComputeBackend
    from vibesys.orchestration.contracts import Orchestrator
    from vs_agent.api import AgentClientProtocol
    from vs_agent.api.testing import FakeAgentClient
    from vs_project.api import OrchestrationDescriptor


class _SharedFakeClient:
    """Give each spawned role handle independent ``close()`` over one script.

    A strategy opens several role handles from the same scripted client; the
    real client type closes per-handle, but the fake script must stay usable
    by every other role until the run ends.
    """

    def __init__(self, scripted: FakeAgentClient) -> None:
        self._scripted = scripted

    def __getattr__(self, name: str) -> object:
        return getattr(self._scripted, name)

    def close(self) -> None:
        """Leave the shared script open for the other roles."""


def _fake_backend_factory(backend: ComputeBackend, **_kwargs: object) -> ComputeBackendImpl:
    """Adapt ``FakeComputeBackend`` to the ``backends.get`` seam's call shape."""
    del backend
    return FakeComputeBackend()


@dataclass(frozen=True, slots=True)
class ScriptedRun:
    """Everything a golden test needs to inspect after a scripted round."""

    result: bool
    workspace: Path
    run_id: str
    events_path: Path


def write_minimal_input_bundle(root: Path, *, domain: str = "llm-serving") -> Path:
    """Write the smallest reference + manifest a scripted run can load.

    A single reference *file* (not a directory) avoids the model-weight
    resolution a reference directory triggers, keeping the fixture
    independent of any developer-host HF cache state.
    """
    model_dir = root / "input_model"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "ref.py").write_text("def predict(x):\n    return x * 2\n")
    (model_dir / "OBJECTIVE.md").write_text("Maximize tok/s throughput.\n")
    (model_dir / "vibesys.input.toml").write_text(
        f"""version = 1

[agent]
domain = "{domain}"

[accuracy]
command = ["python", "-c", "print('ok')"]

[benchmark]
command = ["python", "-c", "print('ok')"]
""",
        encoding="utf-8",
    )
    return model_dir


def run_scripted(  # noqa: PLR0913  # LW-040006 [PLR0913]; the parameters are independent injected collaborators or options, and bundling them would hide ownership.
    tmp_path: Path,
    *,
    orchestration_id: str,
    descriptor: OrchestrationDescriptor,
    orchestrator_factory: type[Orchestrator],
    runner: FakeAgentClient,
    exp_name: str = "golden",
    domain: str = "llm-serving",
    profiler_kind: ProfilerKind = ProfilerKind.NONE,
) -> ScriptedRun:
    """Run one registered strategy end-to-end with a scripted agent client.

    Injects fakes through the run's two construction seams instead of
    patching module attributes: ``backend_factory`` (in place of the CUDA
    backend and its local shell sandbox; trusted gate commands run through
    it, so gate scenarios also script ``run_accuracy_gate``) and
    ``agent_client_factory`` (in place of ``build_agent_client``, the one
    true seam between the host and a real agent CLI). Everything else --
    git tracking, workspace snapshots, progress-board writes, event
    recording -- runs for real against ``tmp_path``.

    ``profiler_kind`` defaults to ``NONE``; pass an active kind to reach a
    strategy's profiler role (the profiler agent is never invoked otherwise).
    """
    input_dir = write_minimal_input_bundle(tmp_path, domain=domain)
    bundle = load_input_bundle(input_dir)
    config = as_config(Config.model_validate({"model": {"name": "claude-golden-test"}}))
    request = RunRequest(
        project_root=bundle.root,
        orchestration=descriptor,
        config=config,
        input_bundle=bundle,
        objective=bundle.objective,
        exp_name=f"{exp_name}-{orchestration_id}",
        runs_dir=tmp_path / "exp_env",
        profiler_kind=profiler_kind,
    )

    # TODO(stack PR 05): pass `projector=built_in_orchestrations().resolve(  # noqa: TD003, FIX002  # LW-040007 [FIX002, TD003]; the placeholder marks work owned by a later change and has no issue yet.
    # orchestration_id).projector` once run_orchestration/RunContext grow the
    # committed-state projection seam.

    async def execute() -> bool:
        integration = LocalRunIntegration()
        try:
            return await run_orchestration(
                request,
                integration,
                orchestrator_factory(descriptor),
                agent_client_factory=cast(
                    "Callable[..., AgentClientProtocol]",
                    lambda **_kwargs: _SharedFakeClient(runner),
                ),
                backend_factory=_fake_backend_factory,
            )
        finally:
            integration.close()

    # test-isolation: PROJECT_ROOT has no injection seam; the run must write under tmp_path
    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        result = asyncio.run(execute())

    projects = [path for path in (tmp_path / "exp_env").iterdir() if path.is_dir()]
    assert len(projects) == 1, f"expected exactly one project directory, found {projects}"
    project_dir = projects[0]
    project = Project.open(project_dir)
    run = project.state.resolve_run()
    events_path = project.state.log_directory(run.run_id) / "core-events.jsonl"
    return ScriptedRun(
        result=result,
        workspace=project_dir,
        run_id=run.run_id,
        events_path=events_path,
    )
