"""Prompt contract tests for plugin-owned evolve templates."""

from vibesys.orchestrations.evolve.models import (
    CandidateJudgeContext,
    CandidateProfilerContext,
    MutatorContext,
)
from vibesys.orchestrations.evolve.prompts import render_judge, render_mutator, render_profiler
from vibesys.profilers import PROFILER_DEFINITIONS


def test_evolve_prompt_resources_render_from_plugin_package() -> None:
    review_criteria = "Preserve correctness."
    mutation = render_mutator(
        MutatorContext(
            accuracy_command=None,
            benchmark_command=None,
            domain_implementer="",
            failed_lessons=[],
            inspirations=[],
            interface="service",
            is_cold_start=True,
            modality=None,
            num_failed_attempts=0,
            objective="Improve throughput.",
            objectives=None,
            parent=None,
            reference_path="reference",
            repair_seed=False,
            runtime_notes="",
        )
    )
    judgment = render_judge(
        CandidateJudgeContext(
            accuracy_command=None,
            benchmark_command=None,
            domain_judge="",
            interface="service",
            modality=None,
            objective="Improve throughput.",
            pass_criteria=review_criteria,
            runtime_notes="",
        )
    )
    profiler_context = CandidateProfilerContext(
        benchmark_command=None,
        domain_profiler="",
        modality=None,
        objective="Improve throughput.",
        pareto_objectives_addendum="",
        profile_execution="Run the profiler.",
        profile_focus="Find the bottleneck.",
        profiler_mcp_name="profiler",
        profiler_support_name="profiler",
        runtime_notes="",
    )
    profiles = [render_profiler(kind.value, profiler_context) for kind in PROFILER_DEFINITIONS]

    assert "mutation operator" in mutation
    assert "senior code reviewer" in judgment
    assert all(profile.strip() for profile in profiles)
