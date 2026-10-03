"""The single plugin's typed prompt contexts and colocated template resources."""

from __future__ import annotations

import inspect
from pathlib import Path

import vibesys.orchestration.single.prompts as single_prompts
from vibesys.orchestration.hypothesis import ExhaustionNotice
from vibesys.orchestration.profile_focus import (
    FocusLedger,
    FocusLedgerRow,
    ProfileGuidanceStatus,
)
from vibesys.orchestration.progress import ProgressLog
from vibesys.orchestration.prompts import PROMPTS_DIR
from vibesys.orchestration.single.models import PlanContext, SingleAgentRoundContext
from vs_prompts.api import resolve_free_variables


def _plan_context(workspace: Path) -> PlanContext:
    log = ProgressLog(workspace, workspace / "progress")
    notice = single_prompts.render_progress(
        "exhaustion",
        round_number=4,
        exhaustion_info=ExhaustionNotice(round_number=3, attempts=2, feedback="Add a test."),
    )
    entry = log.append(4, notice)
    return PlanContext(
        objective_location="OBJECTIVE.md",
        profiler_entry=None,
        regression_entry=entry,
        exhaustion_entry=entry,
        progress_location="progress/ledger.md",
        roadmap_location="progress/roadmap.md",
        pareto_archive_location="progress/pareto.md",
        plateau_warning="The throughput curve has flattened.",
        domain_orchestrator="Domain planning rules.",
        runtime_notes="Use the supplied accelerator.",
        framework_benchmark_enabled=True,
        official_eval_every=3,
        provisional_candidates=2,
        official_eval_cadence_due=True,
        active_component="decode scheduler",
        ledger=FocusLedger(
            rows=(
                FocusLedgerRow(
                    name="decode scheduler",
                    status=ProfileGuidanceStatus.ACTIVE,
                    rounds_spent=1,
                    latest_share=0.43,
                    stalled_rounds=0,
                ),
            )
        ),
        ranked_bottlenecks=[
            {"component": "decode scheduler", "cost_share": 43, "evidence": ["trace.json"]}
        ],
    )


def _single_agent_context(*, interface: str = "service") -> SingleAgentRoundContext:
    return SingleAgentRoundContext(
        accuracy_command="check-accuracy",
        benchmark_command="measure-throughput",
        domain_profiler="Domain profiling rules.",
        domain_single_agent="Domain implementation rules.",
        feedback="Fix the failing health check.",
        interface=interface,
        objective_location="OBJECTIVE.md",
        official_evaluation_due=True,
        official_evaluation_reason="requested by the designer",
        pareto_archive_location="progress/pareto.md",
        plan_artifact_location="progress/plans/round-0001.json",
        profiler_kind="none",
        profiler_support_name=None,
        progress_location="progress/ledger.md",
        runtime_notes="Use the supplied accelerator.",
        validation_location="progress/validation/",
    )


def test_colocated_templates_use_every_field_of_their_typed_contexts() -> None:
    root = Path(inspect.getfile(single_prompts)).parent
    shared = PROMPTS_DIR / "shared"
    for template, model in (
        ("orchestrator_plan_prompt.j2", PlanContext),
        ("single_agent_round_prompt.j2", SingleAgentRoundContext),
    ):
        variables, unresolved = resolve_free_variables(root / template, search_roots=(root, shared))
        assert unresolved == ()
        assert variables == set(model.model_fields)


def test_plan_prompt_renders_policy_context_and_profile_guidance(tmp_path: Path) -> None:
    rendered = single_prompts.render_plan_prompt(_plan_context(tmp_path))

    assert "progress/roadmap.md" in rendered
    assert "The throughput curve has flattened." in rendered
    assert "decode scheduler" in rendered
    assert "decode scheduler | active | 1 | 43.00% | 0" in rendered
    assert "43% of measured cost" in rendered
    assert "latest progress entry contains a regression" in rendered
    assert "latest progress entry contains exhausted-review feedback" in rendered
    assert "The framework has a machine-readable trusted benchmark" in rendered
    assert "Provisional count:\n2; cadence is\ndue." in rendered
    assert "Domain planning rules." in rendered


def test_single_agent_prompt_renders_shared_execution_boundary_and_retry() -> None:
    rendered = single_prompts.render_single_agent_prompt(_single_agent_context())

    assert "Fix the failing health check." in rendered
    assert "requested by the designer" in rendered
    assert "Accuracy: `check-accuracy`" in rendered
    assert "Benchmark: `measure-throughput`" in rendered
    assert "Standalone profiling is disabled" in rendered
    assert "Domain implementation rules." in rendered
    assert "Domain profiling rules." in rendered
    assert "running candidate service" in rendered


def test_single_agent_prompt_renders_inprocess_execution_boundary() -> None:
    context = _single_agent_context(interface="inprocess").model_copy(
        update={"profiler_kind": "torch", "profiler_support_name": "torch_profiler"}
    )

    rendered = single_prompts.render_single_agent_prompt(context)

    assert "invokes the candidate directly inside an evaluator process" in rendered
    assert "Use `torch.profiler` through `torch_profiler`" in rendered
