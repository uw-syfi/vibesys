"""The single plugin's typed prompt contexts and colocated template resources."""

from __future__ import annotations

import inspect
from pathlib import Path

import vibesys.orchestrations.single.prompts as single_prompts
from vibesys.orchestrations.single.models import PlanContext, SingleAgentRoundContext
from vibesys.prompts import PROMPTS_DIR
from vs_prompts.api import resolve_free_variables


def _plan_context() -> PlanContext:
    return PlanContext(
        objective_location="OBJECTIVE.md",
        profiler_summary=None,
        regression_info="The last candidate regressed.",
        exhaustion_info="The last review exhausted its budget.",
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
        ledger_text="decode scheduler: 43%",
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


def test_plan_prompt_renders_policy_context_and_profile_guidance() -> None:
    rendered = single_prompts.render_plan_prompt(_plan_context())

    assert "progress/roadmap.md" in rendered
    assert "The throughput curve has flattened." in rendered
    assert "decode scheduler" in rendered
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
