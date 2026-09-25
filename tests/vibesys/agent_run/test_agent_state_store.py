"""Filesystem contract for the single agent policy state slot."""

from vibesys.agent_run.state import AgentRunState, AgentRunStateStore, Hypothesis
from vibesys.schemas import OrchestratorPlan
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    RunExecutionRecord,
    StateTransition,
)


def _project(tmp_path):  # noqa: ANN001, ANN202
    (tmp_path / "OBJECTIVE.md").write_text("Make it fast.\n")
    project = Project.open(tmp_path)
    project.state.create_project("test")
    manifest = project.state.new_run_manifest(
        "Run 1",
        run_id="run-1",
        trusted_input_baseline="a" * 40,
        branch="vibesys/run-1",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=RunExecutionRecord(
            model="test-model",
            agent_backend="stub",
            compute_backend="cpu",
            requested_profiler="none",
            resolved_profiler="none",
        ),
        orchestration=OrchestrationDescriptor(id="multi-agent", config_version=1, options={}),
    )
    project.state.create_run(manifest)
    return project


def _plan(identifier: str) -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=identifier,
        hypothesis=f"claim {identifier}",
        task=f"implement {identifier}",
        pass_criteria="tests pass",  # noqa: S106
        reasoning="test the claim",
    )


def test_store_round_trips_and_prepares_exact_state_transition(tmp_path) -> None:  # noqa: ANN001
    project = _project(tmp_path)
    namespace = project.state.portable_namespace("run-1", "agent")
    store = AgentRunStateStore(namespace)
    slot = namespace.slot("state.json", AgentRunState)
    state = AgentRunState(
        active_hypothesis_id="H-1",
        hypotheses=[Hypothesis(hypothesis_id="H-1", plan=_plan("H-1"), started_round=1)],
    )

    assert store.load_optional() is None
    transition = slot.transition(state)
    assert isinstance(transition, StateTransition)
    assert store.load_optional() is None

    namespace.apply(transition)
    assert store.load() == state
