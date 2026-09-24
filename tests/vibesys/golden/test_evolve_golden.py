"""Golden snapshots for the evolve strategy: prompts, board/population files,
events.

Drives ``EvolveOrchestrator.run(ctx)`` end-to-end (real workspace, git
tracking, population state, event journal) against a scripted
:class:`FakeAgentClient`, the same integration pattern
``tests/vibesys/loops/evolve/test_evolutionary_loop.py`` uses. No real agent
CLI, sandbox, or subprocess ever runs.

Unlike multi, evolve is generation/population based: a run bootstraps a
generation-0 seed before evolving children off it, and the durable state it
writes is a validated population/metric-space/cursor triple under
``.vibesys/state/runs/<run_id>/evolve/`` rather than markdown progress files.

This module does NOT use ``tests/vibesys/golden/harness.run_scripted``. That
harness hardcodes ``profiler_kind=ProfilerKind.NONE`` (fine for multi, which
never profiles in its golden scenarios) and unconditionally runs the real
mocked-sandbox accuracy gate for every candidate -- evolve calls that gate on
every bootstrap attempt and every child regardless of backend, so both of
those harness defaults break evolve's bootstrap seed (it never profiles, and
the mocked sandbox makes the accuracy gate raise). Neither is exposed as a
``run_scripted`` parameter, and editing ``harness.py`` is out of scope, so
``_run_evolve`` below is a small local adaptation of the same pattern (mirrors
``harness.run_scripted`` and ``test_evolutionary_loop.py``'s ``_invoke_loop``)
that sets ``profiler_kind=ProfilerKind.AUTO`` and leaves the framework
accuracy gate to each scenario to patch explicitly, the same way
``test_evolutionary_loop.py`` patches
``vibesys.loops.evolve.loop._run_framework_accuracy_gate`` directly (evolve
does not route through ``run_accuracy_gate`` the way multi's gate scenario
does, so multi's patch target does not apply here).

Three scenarios cover the main round path:

- ``bootstrap_pass``: bootstrap succeeds first try (one mutator/judge/profiler
  turn), then one generation-1 child is bred off the seed and also passes.
  Two individuals, both profiled, both with real git commits.
- ``bootstrap_retry_then_pass``: the first bootstrap attempt's mutator edit is
  judged FAIL (and, since it changed the tree, is snapshotted to a WIP
  commit); the second attempt repairs it in place and passes, becoming the
  generation-0 seed. Mirrors evolve's actual retry mechanic (fix-forward onto
  the last WIP seed), which is a different shape from multi's
  same-round-retry-with-feedback.
- ``gate``: bootstrap passes, but the generation-1 child's LLM-approved PASS is
  overruled by the trusted framework accuracy gate. The rejected child is
  never profiled or selectable as a future parent; its population record
  carries the gate's feedback and no commit or perf metric.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from tests.vibesys.golden.harness import ScriptedRun, write_minimal_input_bundle
from tests.vibesys.golden.helpers import (
    assert_board_snapshot,
    assert_events_snapshot,
    assert_prompt_snapshot,
    prompt_text,
    read_events,
)

from vibesys.config import Config, as_config
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.evaluators.metrics import MetricSpace
from vibesys.loops.evolve.entrypoint import EvolveOrchestrator
from vibesys.loops.evolve.orchestration import EvolveOptions, descriptor_from_options
from vibesys.orchestration.request import RunRequest
from vibesys.orchestration.runner import run_orchestration
from vibesys.profilers import ProfilerKind
from vibesys.run.integration import LocalRunIntegration
from vibesys.schemas import ImplementerResponse, JudgeResponse, ProfilerSummary, Verdict
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import Project

if TYPE_CHECKING:
    from pathlib import Path

_STRATEGY = "evolve"


class _SharedFakeClient:
    """Give each spawned role handle independent ``close()`` over one script.

    Mirrors ``tests/vibesys/golden/harness._SharedFakeClient``: evolve opens
    several role handles from the same scripted client, and the real client
    type closes per-handle, so the fake script must stay usable by every
    other role until the run ends.
    """

    def __init__(self, scripted: FakeAgentClient) -> None:
        self._scripted = scripted

    def __getattr__(self, name: str) -> object:
        return getattr(self._scripted, name)

    def close(self) -> None:
        """Leave the shared script open for the other roles."""


def _options(**overrides: object) -> EvolveOptions:
    values: dict[str, object] = {
        "modality": "text_generation",
        "max_generations": 1,
        "children_per_generation": 1,
        # 0/0: with only one or two individuals in the population, a nonzero
        # inspiration count can collide with the sampled parent and raise
        # (IndividualRecord forbids the parent also being an inspiration);
        # these golden scenarios don't exercise inspiration selection.
        "k_top_inspirations": 0,
        "k_random_inspirations": 0,
        "selection_temperature": 0.5,
        "seed": 0,
        "frontier_bias": 0.7,
        "bootstrap_max_attempts": 2,
        "keep_deployments": False,
        "max_parallelism": 1,
        "metric_space": MetricSpace(),
    }
    values.update(overrides)
    return EvolveOptions.model_validate(values)


def _implementer(summary: str = "seed") -> ImplementerResponse:
    return ImplementerResponse(
        summary=summary, expected_behavior="higher steady-state throughput", evidence="ran checks"
    )


def _judge(verdict: Verdict, feedback: str = "") -> JudgeResponse:
    return JudgeResponse(analysis="reviewed the candidate", feedback=feedback, verdict=verdict)


def _profiler(perf_metric: float, *, perf_unit: str = "tok/s") -> ProfilerSummary:
    return ProfilerSummary(
        analysis="ok",
        bottlenecks="none",
        suggestions="none",
        perf_metric=perf_metric,
        perf_unit=perf_unit,
    )


def _mutator_writes_callback(fake: FakeAgentClient):  # noqa: ANN202  # tracked: #288
    """Write a distinct file on every mutator turn.

    Without a workspace diff a mutator turn is a no-op snapshot: no git commit
    is recorded, and evolve treats an uncommitted candidate as ineligible (it
    cannot be profiled, selected as a future parent, or reported as the run's
    final tree).
    """

    def _write(call):  # noqa: ANN001, ANN202  # tracked: #288
        if call.kind != "implementer":
            return
        n = len(fake.calls_for("implementer"))
        (call.workspace / f"mutant_{n}.py").write_text(f"# mutant {n}\n")

    return _write


def _run_evolve(
    tmp_path: Path,
    *,
    descriptor,  # noqa: ANN001  # tracked: #288
    runner: FakeAgentClient,
    accuracy_gate: AsyncMock | None = None,
) -> ScriptedRun:
    """Run ``EvolveOrchestrator`` end-to-end with a scripted agent client.

    A local adaptation of ``tests/vibesys/golden/harness.run_scripted`` (see
    module docstring for why): same seams patched (CUDA sandbox factory,
    ``build_agent_client``, ``PROJECT_ROOT``), plus
    ``profiler_kind=ProfilerKind.AUTO`` (evolve's fitness signal) and an
    explicit, scenario-supplied patch of evolve's own framework accuracy-gate
    call site, since every bootstrap attempt and every child unconditionally
    goes through it.
    """
    input_dir = write_minimal_input_bundle(tmp_path, domain="llm-serving")
    bundle = load_input_bundle(input_dir)
    config = as_config(Config.model_validate({"model": {"name": "claude-golden-test"}}))
    request = RunRequest(
        project_root=bundle.root,
        orchestration=descriptor,
        config=config,
        input_bundle=bundle,
        objective=bundle.objective,
        exp_name="golden-evolve",
        runs_dir=tmp_path / "exp_env",
        profiler_kind=ProfilerKind.AUTO,
    )

    async def execute() -> bool:
        integration = LocalRunIntegration()
        try:
            return await run_orchestration(request, integration, EvolveOrchestrator(descriptor))
        finally:
            integration.close()

    gate_patch = accuracy_gate or AsyncMock(return_value=None)
    with (
        patch("vibesys.backends.cuda.make_local_shell_sandbox"),
        patch(
            "vibesys.orchestration.runtime.build_agent_client",
            side_effect=lambda **_kwargs: _SharedFakeClient(runner),
        ),
        patch("vibesys.context.PROJECT_ROOT", tmp_path),
        patch("vibesys.loops.evolve.loop._run_framework_accuracy_gate", gate_patch),
    ):
        result = asyncio.run(execute())

    projects = [path for path in (tmp_path / "exp_env").iterdir() if path.is_dir()]
    assert len(projects) == 1, f"expected exactly one project directory, found {projects}"
    project_dir = projects[0]
    project = Project.open(project_dir)
    run = project.state.resolve_run()
    events_path = project.state.log_directory(run.run_id) / "core-events.jsonl"
    return ScriptedRun(
        result=result, workspace=project_dir, run_id=run.run_id, events_path=events_path
    )


def test_bootstrap_pass_scenario_golden(tmp_path: Path) -> None:
    """Bootstrap passes first try; one generation-1 child also passes."""
    runner = FakeAgentClient()
    runner.on_invoke(_mutator_writes_callback(runner))
    runner.enqueue("implementer", _implementer("bootstrap seed"), _implementer("gen-1 child"))
    runner.enqueue("judge", _judge(Verdict.PASS), _judge(Verdict.PASS))
    runner.enqueue("profiler", _profiler(10.0), _profiler(11.0))

    descriptor = descriptor_from_options(_options())
    run = _run_evolve(tmp_path, descriptor=descriptor, runner=runner)

    assert run.result is True
    _assert_prompt_calls(runner, scenario="bootstrap_pass", workspace=tmp_path.parent)
    _assert_population_files(run.workspace, scenario="bootstrap_pass", workspace=tmp_path.parent)
    assert_events_snapshot(
        _STRATEGY, "bootstrap_pass", read_events(run.events_path, workspace=tmp_path.parent)
    )


def test_bootstrap_retry_then_pass_scenario_golden(tmp_path: Path) -> None:
    """First bootstrap attempt is judged FAIL and snapshotted as a WIP commit;
    the second attempt repairs it in place and becomes the generation-0 seed.
    """
    runner = FakeAgentClient()
    runner.on_invoke(_mutator_writes_callback(runner))
    runner.enqueue(
        "implementer",
        _implementer("first attempt: incomplete"),
        _implementer("second attempt: repaired"),
    )
    runner.enqueue(
        "judge",
        _judge(Verdict.FAIL, feedback="throughput claim is unsubstantiated"),
        _judge(Verdict.PASS),
    )
    runner.enqueue("profiler", _profiler(10.0))

    descriptor = descriptor_from_options(_options(bootstrap_max_attempts=2))
    run = _run_evolve(tmp_path, descriptor=descriptor, runner=runner)

    assert run.result is True
    _assert_prompt_calls(runner, scenario="bootstrap_retry_then_pass", workspace=tmp_path.parent)
    _assert_population_files(
        run.workspace, scenario="bootstrap_retry_then_pass", workspace=tmp_path.parent
    )
    assert_events_snapshot(
        _STRATEGY,
        "bootstrap_retry_then_pass",
        read_events(run.events_path, workspace=tmp_path.parent),
    )


def test_gate_scenario_golden(tmp_path: Path) -> None:
    """The generation-1 child is LLM-approved but the trusted framework
    accuracy gate overrules it: it is rejected, never profiled, and carries
    no commit or perf metric in the population record.
    """
    runner = FakeAgentClient()
    runner.on_invoke(_mutator_writes_callback(runner))
    runner.enqueue("implementer", _implementer("bootstrap seed"), _implementer("gen-1 child"))
    runner.enqueue("judge", _judge(Verdict.PASS), _judge(Verdict.PASS))
    runner.enqueue("profiler", _profiler(10.0))

    rejection = "Framework accuracy gate failed.\nbenchmark endpoint diverged from the reference"
    accuracy_gate = AsyncMock(side_effect=[None, rejection])

    descriptor = descriptor_from_options(_options())
    run = _run_evolve(tmp_path, descriptor=descriptor, runner=runner, accuracy_gate=accuracy_gate)

    assert run.result is True
    _assert_prompt_calls(runner, scenario="gate", workspace=tmp_path.parent)
    _assert_population_files(run.workspace, scenario="gate", workspace=tmp_path.parent)
    assert_events_snapshot(
        _STRATEGY, "gate", read_events(run.events_path, workspace=tmp_path.parent)
    )


def _assert_prompt_calls(runner: FakeAgentClient, *, scenario: str, workspace: Path) -> None:
    for index, call in enumerate(runner.calls, start=1):
        role = f"{call.kind}-{index:02d}-{call.round_label}"
        assert_prompt_snapshot(
            _STRATEGY,
            role,
            scenario,
            prompt_text(call.system_prompt, call.user_prompt),
            workspace=workspace,
        )


_POPULATION_FILES = ("population.json", "metrics.json", "generation.json")


def _assert_population_files(project_dir: Path, *, scenario: str, workspace: Path) -> None:
    state_dirs = sorted((project_dir / ".vibesys" / "state" / "runs").glob("*/evolve"))
    assert len(state_dirs) == 1, state_dirs
    state_dir = state_dirs[0]
    for relative in _POPULATION_FILES:
        path = state_dir / relative
        if not path.exists():
            continue
        assert_board_snapshot(_STRATEGY, scenario, relative, path.read_text(), workspace=workspace)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
