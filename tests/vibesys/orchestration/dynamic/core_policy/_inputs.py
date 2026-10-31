"""What the host holds before a dynamic run starts, and the core run context made from it."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, TypedDict, Unpack

from tests.support.task_project import write_task

from vibesys.core_wiring import ResolvedRun
from vibesys.dynamic_handoff import HandoffOwner
from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.launch import DynamicLaunch
from vibesys.orchestration.dynamic.search.api import HANDOFF
from vibesys.prompts import CORE_PROTOCOL_DIR
from vs_core.api import RequestRetries, RevisionRef
from vs_project.api import Project
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import ArtifactStore, CorePlan, CoreRunContext, RunFacts
from vs_runtime.api.core import KeyedWrite, OperationEntry, OperationPorts
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
OPTIONS: dict[str, object] = {"interface": "inprocess", "max_rounds": 2, "max_in_flight": 2}


def launch_of(options: Mapping[str, object] | None = None) -> DynamicLaunch:
    """The launch options of a run: the defaults of `OPTIONS`, overridden by ``options``."""
    return DynamicLaunch.model_validate({**OPTIONS, **(options or {})})


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


class HostFacts(TypedDict, total=False):
    """What the host resolved of the run environment and the product config; a test varies one."""

    queue_allowance_seconds: int
    observe: tuple[int, int]
    """The poll interval and the backoff cap, in seconds."""
    max_run_seconds: int | None
    evaluation_capacity: int


def context_of(
    run: ResolvedRun, options: Mapping[str, object] | None = None, **host: Unpack[HostFacts]
) -> CoreRunContext:
    """The context the host hands the dynamic policy for ``run``, launched with ``options``."""
    interval, backoff_cap = host.get("observe", (10, 120))
    return CoreRunContext(
        run_id="policy",
        options=launch_of(options),
        facts=run.facts,
        baseline=RevisionRef.of_git_commit(run.baseline_commit),
        evaluation_plan=run.evaluation_plan,
        environment=run.environment,
        artifacts=run.artifacts,
        evaluation_capacity=host.get("evaluation_capacity", EVALUATION_CAPACITY),
        queue_allowance_seconds=host.get("queue_allowance_seconds", 900),
        observe_interval_seconds=interval,
        observe_backoff_cap_seconds=backoff_cap,
        queue_wait_budget_seconds=43_200,
        queue_notice_interval_seconds=600,
        request_retries=RequestRetries(limit=2, backoff=1.0, backoff_cap=8.0),
        max_run_seconds=host.get("max_run_seconds"),
        cancellation_bound_seconds=60,
        cleanup_bound_seconds=30,
        reconciliation_bound_seconds=60,
    )


def plan_of(
    run: ResolvedRun, options: Mapping[str, object] | None = None, **host: Unpack[HostFacts]
) -> CorePlan:
    """What the dynamic policy plans for ``run``."""
    assert PLUGIN.core is not None
    return PLUGIN.core.plan(context_of(run, options, **host))


class MemoryDocuments:
    """Handoff documents kept in memory under the project-relative root a namespace would have."""

    def __init__(self, root: str = ".vibesys/state/runs/run/orchestrator-handoff") -> None:
        """An empty store whose documents an agent sees below ``root``."""
        self.root = root
        self.files: dict[str, bytes] = {}

    def read_bytes(self, relative_path: str | PurePosixPath) -> bytes | None:
        """One document's bytes, or None when it is absent."""
        return self.files.get(str(relative_path))

    def write_bytes(self, relative_path: str | PurePosixPath, contents: bytes) -> None:
        """Replace one document."""
        self.files[str(relative_path)] = contents

    def agent_visible_path(self, relative_path: str | PurePosixPath | None = None) -> str:
        """The document's project-relative path."""
        return self.root if relative_path is None else f"{self.root}/{relative_path}"

    def entries(self, relative_directory: str | PurePosixPath) -> tuple[str, ...]:
        """The names of the documents, sorted; the store has one directory."""
        prefix = f"{relative_directory}/"
        return tuple(
            sorted(name.removeprefix(prefix) for name in self.files if name.startswith(prefix))
        )


def handoff() -> tuple[OperationEntry, ...]:
    """The handoff write, owned by the production owner over in-memory documents."""
    owner = HandoffOwner(MemoryDocuments())
    return (OperationEntry.owned(HANDOFF, owner, KeyedWrite("the handoff file of the call")),)


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
