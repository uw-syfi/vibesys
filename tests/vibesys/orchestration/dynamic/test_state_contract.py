"""One durability contract for dynamic state, run against the Fake and the real store.

Dynamic unit tests use ``FakeState``; the product uses the runtime's checkpointed
store. Every case here runs against both, so a Fake that is laxer than
production fails here instead of hiding a crash from the unit tests.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ConfigDict

from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.inputs import load_input_bundle
from vibesys.orchestration.dynamic.models import (
    DynamicState,
    DynamicWorkstream,
    EvaluationResult,
    ImplementerResult,
    ReviewResult,
    WorkstreamBudget,
    WorkstreamPhase,
    WorkstreamPlan,
)
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.run.contracts import RunRequest
from vibesys.run.host import open_product_run_host
from vibesys.run.integration import LocalRunIntegration
from vs_loop_state.api import HypothesisOutcome
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api import (
    MetricDirection,
    OrchestrationPlugin,
    Run,
    RunStatus,
    RuntimeContractError,
    StateModelError,
)
from vs_runtime.api.testing import FakeRun, FakeWorkspace

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from vs_runtime.api import State, Workspace


class _Options(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


async def _orchestrate(run: Run, options: BaseModel) -> RunStatus:
    del run, options
    return RunStatus.SUCCEEDED


_PLUGIN = OrchestrationPlugin(
    id="dynamic-state-contract",
    agents=(),
    options=_Options,
    orchestrate=_orchestrate,
    state=DynamicState,
)

type _Store = Callable[[Path], AbstractAsyncContextManager[tuple[State, Workspace]]]


@asynccontextmanager
async def _fake(tmp_path: Path) -> AsyncIterator[tuple[State, Workspace]]:
    run = FakeRun(_PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    yield run.state, run.workspaces.root


@asynccontextmanager
async def _real(tmp_path: Path) -> AsyncIterator[tuple[State, Workspace]]:
    root = tmp_path / "project"
    root.mkdir()
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )
    request = RunRequest(
        project_root=root,
        orchestration=OrchestrationDescriptor(id=_PLUGIN.id, config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "state-contract"}}),
        input_bundle=load_input_bundle(root),
        objective="Improve the queue.",
        exp_name="state-contract",
        agent_backend="stub",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )
    async with open_product_run_host(request, LocalRunIntegration(), plugin=_PLUGIN) as run:
        yield run.state, run.workspaces.root


STORES = pytest.mark.parametrize(
    "store",
    [
        pytest.param(_fake, id="fake"),
        pytest.param(_real, id="real", marks=pytest.mark.real_contract),
    ],
)


def _workstream(
    sequence: int, hypothesis_id: str, phase: WorkstreamPhase, *, text: str = ""
) -> DynamicWorkstream:
    plan = WorkstreamPlan.model_validate(
        {
            "hypothesis_id": hypothesis_id,
            "title": "T" * 80 + text,
            "hypothesis": "Batching amortizes the lock." + text,
            "task": "Batch the queue." + text,
            "pass_criteria": "Throughput rises." + text,
            "evidence": [{"location": "queue.py:1", "purpose": "hot path", "revision": "abc"}],
        }
    )
    finished = phase not in {WorkstreamPhase.PENDING, WorkstreamPhase.IMPLEMENTING}
    return DynamicWorkstream(
        hypothesis_id=hypothesis_id,
        sequence=sequence,
        planning_call=1,
        plan=plan,
        parent_revision="0" * 40,
        phase=phase,
        budget=WorkstreamBudget(spent=1, refunded=1),
        candidate_revision="1" * 40 if finished else None,
        implementation=(
            ImplementerResult(
                summary="Batched." + text,
                outcome=HypothesisOutcome.SUPPORTED,
                next_step=text,
            )
            if finished
            else None
        ),
        review=ReviewResult(passed=False, analysis="No." + text, feedback="Retry." + text)
        if finished
        else None,
        evaluation=(
            EvaluationResult(
                revision="1" * 40,
                accuracy_passed=True,
                benchmark_passed=True,
                metric_name="throughput",
                metric_value=2.5,
                metric_direction=MetricDirection.MAXIMIZE,
                metrics={"throughput": 2.5},
            )
            if phase is WorkstreamPhase.EVALUATED
            else None
        ),
        last_error="agent CLI exited" if phase is WorkstreamPhase.FAILED else None,
        implementer_started=phase is not WorkstreamPhase.PENDING,
    )


_IDS = ("H1", "KV.Cache_v2 / ../Ünïcode", "x" * 128, "a b", "h1", "Ω")
REPRESENTATIVE = (
    DynamicState(),
    DynamicState(
        workstreams=[
            _workstream(index + 1, hypothesis_id, phase)
            for index, (hypothesis_id, phase) in enumerate(zip(_IDS, WorkstreamPhase, strict=True))
        ],
        next_planning_call=3,
        winner_revision="1" * 40,
        adoption_pending=True,
    ),
    # Agent free text has no length cap, so a long value must persist and reload.
    DynamicState(
        workstreams=[_workstream(1, "H1", WorkstreamPhase.EVALUATED, text=" long" * 20_000)]
    ),
)


def _exercise(
    store: _Store, tmp_path: Path, check: Callable[[State, Workspace], Awaitable[None]]
) -> None:
    async def run() -> None:
        async with store(tmp_path) as (state, root):
            await check(state, root)

    asyncio.run(run())


@STORES
def test_nothing_loads_before_the_first_commit(store: _Store, tmp_path: Path) -> None:
    async def check(state: State, _root: Workspace) -> None:
        assert await state.load(DynamicState) is None

    _exercise(store, tmp_path, check)


@STORES
@pytest.mark.parametrize("value", REPRESENTATIVE, ids=("empty", "every-phase", "long-text"))
def test_a_committed_state_loads_equal_and_detached(
    store: _Store, tmp_path: Path, value: DynamicState
) -> None:
    async def check(state: State, root: Workspace) -> None:
        original = value.model_copy(deep=True)
        await state.commit(original, workspace=root, label="contract")
        original.next_planning_call += 1

        loaded = await state.load(DynamicState)
        assert loaded == value
        assert loaded is not None
        loaded.workstreams.clear()
        assert await state.load(DynamicState) == value

    _exercise(store, tmp_path, check)


class _Other(BaseModel):
    value: int = 0


@STORES
def test_another_model_is_rejected(store: _Store, tmp_path: Path) -> None:
    async def check(state: State, _root: Workspace) -> None:
        with pytest.raises(StateModelError):
            await state.load(_Other)
        with pytest.raises(StateModelError):
            await state.commit(_Other())

    _exercise(store, tmp_path, check)


@STORES
def test_only_the_root_workspace_can_be_committed(store: _Store, tmp_path: Path) -> None:
    async def check(state: State, _root: Workspace) -> None:
        with pytest.raises(RuntimeContractError):
            await state.commit(DynamicState(), workspace=FakeWorkspace(path=tmp_path / "member"))

    _exercise(store, tmp_path, check)
