"""Real run requests and the resolved inputs a host would hold for them."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vibesys.api import ComputeBackend, Config, ProfilerKind, RunRequest
from vibesys.api.request import load_input_bundle
from vibesys.dynamic_core import ResolvedRun
from vibesys.orchestration.dynamic import PLUGIN, REGISTRATION
from vibesys.orchestration.dynamic.core_policy.api import RunBounds
from vs_project.api import OrchestrationDescriptor, Project
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import ArtifactStore, RunFacts
from vs_runtime.api.core import OperationPorts
from vs_runtime.api.infrastructure import AgentPaths, RunEnvironmentView, TrustedEvaluationPlan
from vs_runtime.api.testing import FakeEvidenceLedger, FakeWorkspace, FakeWorkspaces

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel, JsonValue

COMMIT = "0123456789abcdef0123456789abcdef01234567"


def write_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "OBJECTIVE.md").write_text("Make the queue faster.\n")
    (root / "benchmark.py").write_text("print('ok')\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n'
        f"[benchmark]\ncommand = {json.dumps(['python', 'benchmark.py'])}\n"
    )


# Every required option of the dynamic plugin; a test overrides what it varies.
OPTIONS: dict[str, JsonValue] = {
    "interface": "inprocess",
    "max_rounds": 2,
    "max_in_flight": 2,
    "max_retries_per_round": 1,
    "judge_every": 1,
    "official_eval_every": 1,
}


def run_request(
    root: Path, options: dict[str, JsonValue] | None = None, config: dict[str, object] | None = None
) -> RunRequest:
    write_project(root)
    return RunRequest(
        project_root=root,
        orchestration=OrchestrationDescriptor(
            id=PLUGIN.id,
            config_version=PLUGIN.config_version,
            options={**OPTIONS, **(options or {})},
        ),
        config=Config.model_validate({"model": {"name": "test"}, **(config or {})}),
        input_bundle=load_input_bundle(root),
        objective="Make the queue faster.",
        exp_name="policy",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def options_of(request: RunRequest) -> BaseModel:
    """Parse the descriptor through the registration, as the launch does."""
    return REGISTRATION.parse_options(request.orchestration)


def bounds_of(request: RunRequest) -> RunBounds:
    return RunBounds(
        queue_allowance_seconds=request.config.evaluation.queue_allowance_seconds,
        observe_interval_seconds=request.config.evaluation.observe_interval_seconds,
        observe_backoff_cap_seconds=request.config.evaluation.observe_backoff_cap_seconds,
        max_run_seconds=request.config.run.max_run_seconds,
    )


def resolved(
    root: Path, *, profiler_id: str = "none", plan: TrustedEvaluationPlan | None = None
) -> ResolvedRun:
    project = Project.open(root)
    store = ArtifactStore(project.state.state_store_namespace("policy"))
    return ResolvedRun(
        facts=RunFacts(
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
        environment=RunEnvironmentView(paths=AgentPaths()),
        baseline_commit=COMMIT,
        artifacts=store,
    )


def ports(root: Path, run: ResolvedRun) -> OperationPorts:
    """The runtime ports of the production owners, over the in-memory workspace fake."""
    workspaces = FakeWorkspaces(FakeWorkspace())
    return OperationPorts(
        renderer=TemplateRenderer(root),
        artifacts=run.artifacts,
        workspaces=workspaces,
        ledger=workspaces,
        evidence=FakeEvidenceLedger(),
        commit_of=lambda revision: revision.git_commit,
        retention_label="verified",
    )
