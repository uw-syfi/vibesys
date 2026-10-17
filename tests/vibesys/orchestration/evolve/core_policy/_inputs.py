"""What the host holds before an evolve run starts, and the core run context made from it."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from tests.support.task_project import write_task

from vibesys.core_wiring import ResolvedRun
from vibesys.orchestration.evolve import PLUGIN
from vibesys.orchestration.evolve.launch import EvolveLaunch
from vibesys.prompts import CORE_PROTOCOL_DIR
from vs_core.api import RequestRetries, RevisionRef
from vs_project.api import Project
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import ArtifactStore, CorePlan, CoreRunContext, RunFacts
from vs_runtime.api.core import OperationPorts
from vs_runtime.api.infrastructure import AgentPaths, RunEnvironmentView, TrustedEvaluationPlan
from vs_runtime.api.testing import (
    FakeEvidenceLedger,
    FakeWorkspace,
    FakeWorkspaces,
    MemoryValidationPasses,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

COMMIT = "0123456789abcdef0123456789abcdef01234567"
EVALUATION_CAPACITY = 1


def write_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "benchmark.py").write_text("print('ok')\n")
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n'
        f"[benchmark]\ncommand = {json.dumps(['python', 'benchmark.py'])}\n",
        objective="Make the queue faster.\n",
    )


# Every required launch option; a test overrides what it varies.
OPTIONS: dict[str, object] = {
    "max_generations": 2,
    "children_per_generation": 2,
    "k_top_inspirations": 1,
    "k_random_inspirations": 1,
    "selection_temperature": 1.0,
    "frontier_bias": 0.7,
    "bootstrap_max_attempts": 2,
    "max_parallelism": 2,
}


def launch_of(options: Mapping[str, object] | None = None) -> EvolveLaunch:
    """The launch options of a run: the defaults of `OPTIONS`, overridden by ``options``."""
    return EvolveLaunch.model_validate({**OPTIONS, **(options or {})})


def candidate_environment(**changes: object) -> RunEnvironmentView:
    """A run environment that opens a sandbox per candidate, with ``changes`` applied."""
    return replace(
        RunEnvironmentView(paths=AgentPaths(), workspace_git_writable=True),
        **{"env_kind": "docker", "cli_sandboxed": True, "share_agent_session": False, **changes},
    )


def resolved(
    root: Path,
    *,
    profiler_id: str = "none",
    plan: TrustedEvaluationPlan | None = None,
    environment: RunEnvironmentView | None = None,
) -> ResolvedRun:
    project = Project.open(root)
    store = ArtifactStore(project.state.state_store_namespace("policy"))
    return ResolvedRun(
        facts=RunFacts(
            agent_git_writable=True,
            domain_id="generic",
            objective="Make the queue faster.",
            accuracy_configured=True,
            benchmark_configured=True,
            profiler_id=profiler_id,
        ),
        evaluation_plan=plan
        or TrustedEvaluationPlan(
            accuracy_command="true",
            accuracy_timeout_seconds=120,
            benchmark_command="python benchmark.py",
            benchmark_timeout_seconds=600,
        ),
        environment=environment or candidate_environment(),
        baseline_commit=COMMIT,
        artifacts=store,
    )


def context_of(
    run: ResolvedRun,
    options: Mapping[str, object] | None = None,
    *,
    queue_allowance_seconds: int = 900,
    observe: tuple[int, int] = (10, 120),
    max_run_seconds: int | None = None,
) -> CoreRunContext:
    """The context the host hands the evolve policy for ``run``, launched with ``options``."""
    return CoreRunContext(
        run_id="policy",
        options=launch_of(options),
        facts=run.facts,
        baseline=RevisionRef.of_git_commit(run.baseline_commit),
        evaluation_plan=run.evaluation_plan,
        environment=run.environment,
        artifacts=run.artifacts,
        evaluation_capacity=EVALUATION_CAPACITY,
        queue_allowance_seconds=queue_allowance_seconds,
        observe_interval_seconds=observe[0],
        observe_backoff_cap_seconds=observe[1],
        queue_wait_budget_seconds=43_200,
        queue_notice_interval_seconds=600,
        request_retries=RequestRetries(limit=2, backoff=1.0, backoff_cap=8.0),
        max_run_seconds=max_run_seconds,
        cancellation_bound_seconds=60,
        cleanup_bound_seconds=30,
        reconciliation_bound_seconds=60,
    )


def plan_of(
    run: ResolvedRun,
    options: Mapping[str, object] | None = None,
    *,
    queue_allowance_seconds: int = 900,
    observe: tuple[int, int] = (10, 120),
    max_run_seconds: int | None = None,
) -> CorePlan:
    """What the evolve policy plans for ``run``."""
    assert PLUGIN.core is not None
    return PLUGIN.core.plan(
        context_of(
            run,
            options,
            queue_allowance_seconds=queue_allowance_seconds,
            observe=observe,
            max_run_seconds=max_run_seconds,
        )
    )


def ports(root: Path, run: ResolvedRun) -> OperationPorts:
    """The runtime ports of the production owners, over the in-memory workspace fake."""
    workspaces = FakeWorkspaces(FakeWorkspace(path=Path("/workspace")))
    return OperationPorts(
        renderer=TemplateRenderer(root, fallback_roots=(CORE_PROTOCOL_DIR,)),
        artifacts=run.artifacts,
        workspaces=workspaces,
        ledger=workspaces,
        evidence=FakeEvidenceLedger(),
        commit_of=lambda revision: revision.git_commit,
        retention_label="verified",
        validation_passes=MemoryValidationPasses(),
    )
