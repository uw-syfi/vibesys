"""A seeded saved run for the experiment-chat tests: one project, one run, one proven idea."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from tests.support.multi_search import SearchRecord

from vibesys.api.hypothesis import RoundRecord
from vibesys.hypothesis import OrchestratorPlan
from vibesys.hypothesis.state import Hypothesis, HypothesisReview, HypothesisState
from vibesys.orchestration.agent_options import AgentOrchestrationOptions
from vs_project.api import (
    AgentRoleExecutionRecord,
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    RunExecutionRecord,
)

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 8, 11, 12, 34, 56, tzinfo=UTC)
UNIQUE = UUID("12345678-1234-5678-1234-567812345678")


def configuration() -> AgentOrchestrationOptions:
    return AgentOrchestrationOptions(
        interface="inprocess",
        max_rounds=10,
        max_retries_per_round=3,
        judge_every=3,
        modality="text_generation",
        operator_constraints=("Do not change the ABI",),
    )


def project_with_run(tmp_path: Path) -> tuple[Project, str]:
    (tmp_path / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    project = Project.open(tmp_path)
    project.state.create_project("Queue SPSC", now=NOW)
    manifest = project.state.new_run_manifest(
        "Queue SPSC",
        branch="vibesys/queue",
        vibesys_version="0.2.0",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=RunExecutionRecord(
            model="gpt-5",
            agent_backend="cli",
            cli_provider="codex",
            cli_timeout=1800,
            compute_backend="cpu",
            requested_profiler="linux-cpu",
            resolved_profiler="linux-cpu",
            default_reasoning_effort="high",
            agent_roles={
                "orchestrator": AgentRoleExecutionRecord(
                    model="gpt-5.6-sol", reasoning_effort="xhigh"
                ),
                "implementer": AgentRoleExecutionRecord(
                    model="gpt-5.6-luna", reasoning_effort="medium"
                ),
            },
        ),
        orchestration=OrchestrationDescriptor(
            id="multi-agent",
            config_version=1,
            options=configuration().model_dump(mode="json"),
        ),
        trusted_input_baseline="a" * 40,
        now=NOW,
        unique=UNIQUE,
    )
    project.state.create_run(manifest)
    return project, manifest.run_id


def hypothesis() -> Hypothesis:
    return Hypothesis(
        hypothesis_id="H-01",
        started_round=1,
        plan=OrchestratorPlan(
            hypothesis_id="H-01",
            title="Batch decode requests",
            hypothesis="Batching improves throughput.",
            task="Add request batching.",
            pass_criteria="",
            reasoning="",
        ),
        rounds=[
            RoundRecord(
                round_number=1,
                commit="c1",
                perf_metric=100.0,
                perf_unit="ops_s",
                perf_provenance="implementer",
                passed=True,
                hypothesis_id="H-01",
                judge_verdict="pass",
                hypothesis_outcome="proven",
            ),
        ],
        review=HypothesisReview.PASS,
    )


def seed_agent_state(project: Project, run_id: str) -> None:
    SearchRecord(project, run_id).save(
        HypothesisState(hypotheses=[hypothesis()], active_hypothesis_id="H-01")
    )
