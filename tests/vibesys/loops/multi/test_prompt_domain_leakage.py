"""Regression tests for keeping domain-specific prompt knowledge scoped.

The checks are data-driven on purpose: add one ``DomainLeakCheck`` when a domain
gets new distinctive terminology, or add target domains to vet more neutral
packs against an existing keyword set.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # noqa: TC003  # tracked: #288

import pytest

from vibesys.constants import DomainName
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_section
from vibesys.orchestration import memory
from vibesys.profilers import ProfilerKind, profiler_definition
from vibesys.prompts import PROMPTS_DIR, render_template

_TEMPLATE_DIR = PROMPTS_DIR / "loops" / "multi"
_SINGLE_TEMPLATE_DIR = PROMPTS_DIR / "loops" / "single"


@dataclass(frozen=True)
class DomainLeakCheck:
    """Terms from ``source_domain`` that must not appear in ``target_domains``."""

    source_domain: DomainName
    target_domains: tuple[DomainName, ...]
    keywords: tuple[str, ...]
    modality: str | None = None


DOMAIN_LEAK_CHECKS = (
    DomainLeakCheck(
        source_domain=DomainName.LLM_SERVING,
        target_domains=(DomainName.GENERIC,),
        keywords=(
            "FastAPI",
            "causal LM",
            "VibeServeModel",
            "/v1/completions",
            "OpenAI-compatible",
            "serving-systems",
            "model weights",
            "/model",
            "decode invariants",
            "continuous batching",
            "KV cache",
            "FlashInfer",
            "FlashAttention",
            "CUDA graphs",
            "EAGLE",
            "xgrammar",
            "speculative decoding",
            "Modal",
            "modal_profile",
        ),
    ),
)

_NEUTRAL_CONTEXT: dict[str, object] = {
    "modality": None,
    "interface": "inprocess",
    "reference_path": "/workspace/reference/main.py",
    "benchmark_command": "uv run python benchmark/benchmark.py",
    "accuracy_command": "uv run python accuracy_checker/checker.py",
    "runtime_notes": "",
    "task": "TASK: optimize a queue implementation for steady-state throughput.",
    "pass_criteria": "PASS: preserve FIFO behavior and improve the benchmark headline metric.",
    "objective": "OBJECTIVE: maximize operations per second for the queue benchmark.",
    "roadmap_text": "- major-1: todo - identify the next data-structure bottleneck.",
    "profile_execution": "local",
}

_PRIOR_SOLUTION_TERMS = (
    "EAGLE3",
    "speculative decoding",
    "CUDA graphs",
    "FlashAttention",
    "continuous batching",
    "paged attention",
)


def _domain_context(context: dict[str, object]) -> dict[str, object]:
    return {
        "modality": context["modality"],
        "interface": context["interface"],
        "reference_path": context["reference_path"],
        "benchmark_command": context["benchmark_command"],
        "accuracy_command": context["accuracy_command"],
        "runtime_notes": context["runtime_notes"],
        "profile_execution": context["profile_execution"],
        "workspace_sources": (),
    }


def test_fresh_roadmap_scaffold_does_not_seed_solution_ideas(tmp_path: Path) -> None:
    roadmap = tmp_path / "roadmap"
    memory.ensure_roadmap_file(roadmap)

    text = (roadmap / "index.md").read_text()
    for term in _PRIOR_SOLUTION_TERMS:
        assert term.casefold() not in text.casefold()


def _domain_section(domain: DomainName, role: str, context: dict[str, object]) -> str:
    return render_domain_section(resolve_domain(domain), role, **_domain_context(context))


def _render_prompt_bundle(domain: DomainName, *, modality: str | None) -> dict[str, str]:
    context = _NEUTRAL_CONTEXT | {"modality": modality}
    return {
        "implementer": render_template(
            "implementer_prompt.j2",
            template_dir=_TEMPLATE_DIR,
            modality=context["modality"],
            interface=context["interface"],
            reference_path=context["reference_path"],
            runtime_notes=context["runtime_notes"],
            task=context["task"],
            pass_criteria=context["pass_criteria"],
            feedback=None,
            prior_attempt_artifact_locations=(),
            recommended_skills=[],
            domain_implementer=_domain_section(domain, "implementer", context),
        ),
        "judge": render_template(
            "judge_prompt.j2",
            template_dir=_TEMPLATE_DIR,
            modality=context["modality"],
            interface=context["interface"],
            objective=context["objective"],
            pass_criteria=context["pass_criteria"],
            runtime_notes=context["runtime_notes"],
            benchmark_command=context["benchmark_command"],
            accuracy_command=context["accuracy_command"],
            domain_judge=_domain_section(domain, "judge", context),
            pareto_archive_conflict=None,
        ),
        "single_agent_nsys": render_template(
            "single_agent_round_prompt.j2",
            template_dir=_SINGLE_TEMPLATE_DIR,
            modality=context["modality"],
            interface=context["interface"],
            profile_execution=context["profile_execution"],
            objective=context["objective"],
            runtime_notes=context["runtime_notes"],
            task=context["task"],
            pass_criteria=context["pass_criteria"],
            benchmark_command=context["benchmark_command"],
            accuracy_command=context["accuracy_command"],
            retry=1,
            feedback=None,
            reference_path=context["reference_path"],
            profiler_kind=ProfilerKind.NSYS,
            profiler_support_name=profiler_definition(ProfilerKind.NSYS).support_name,
            profiler_mcp_name=profiler_definition(ProfilerKind.NSYS).mcp_name,
            profile_focus="",
            domain_single_agent=_domain_section(domain, "single_agent", context),
            domain_profiler=_domain_section(domain, "profiler", context),
        ),
        "single_agent_torch": render_template(
            "single_agent_round_prompt.j2",
            template_dir=_SINGLE_TEMPLATE_DIR,
            modality=context["modality"],
            interface=context["interface"],
            profile_execution=context["profile_execution"],
            objective=context["objective"],
            runtime_notes=context["runtime_notes"],
            task=context["task"],
            pass_criteria=context["pass_criteria"],
            benchmark_command=context["benchmark_command"],
            accuracy_command=context["accuracy_command"],
            retry=1,
            feedback=None,
            reference_path=context["reference_path"],
            profiler_kind=ProfilerKind.TORCH,
            profiler_support_name=profiler_definition(ProfilerKind.TORCH).support_name,
            profiler_mcp_name=profiler_definition(ProfilerKind.TORCH).mcp_name,
            profile_focus="",
            domain_single_agent=_domain_section(domain, "single_agent", context),
            domain_profiler=_domain_section(domain, "profiler", context),
        ),
        "orchestrator_pre_round": render_template(
            "orchestrator_pre_round_prompt.j2",
            template_dir=_TEMPLATE_DIR,
            objective=context["objective"],
            regression_info=None,
            exhaustion_info=None,
        ),
        "orchestrator_plan": render_template(
            "orchestrator_plan_prompt.j2",
            template_dir=_TEMPLATE_DIR,
            objective=context["objective"],
            profiler_summary=None,
            regression_info=None,
            exhaustion_info=None,
            roadmap_text=context["roadmap_text"],
            plateau_warning=None,
            runtime_notes=context["runtime_notes"],
            profile_execution=context["profile_execution"],
            domain_orchestrator=_domain_section(domain, "orchestrator", context),
        ),
        "profiler_nsys": render_template(
            "profilers/nsys.j2",
            template_dir=_TEMPLATE_DIR,
            profile_focus="queue benchmark hotspots",
            benchmark_command=context["benchmark_command"],
            modality=context["modality"],
            domain_profiler=_domain_section(domain, "profiler", context),
            runtime_notes=context["runtime_notes"],
            profile_execution=context["profile_execution"],
            objective=context["objective"],
            profiler_support_name="nsys_profiler",
            profiler_mcp_name="vibesys-nsys-profiler",
            profiler_campaign_context="",
        ),
        "profiler_torch": render_template(
            "profilers/torch.j2",
            template_dir=_TEMPLATE_DIR,
            profile_focus="queue benchmark hotspots",
            benchmark_command=context["benchmark_command"],
            modality=context["modality"],
            domain_profiler=_domain_section(domain, "profiler", context),
            runtime_notes=context["runtime_notes"],
            profile_execution=context["profile_execution"],
            objective=context["objective"],
            profiler_support_name="torch_profiler",
            profiler_mcp_name="vibesys-torch-profiler",
            profiler_campaign_context="",
        ),
        "profiler_neuron": render_template(
            "profilers/neuron.j2",
            template_dir=_TEMPLATE_DIR,
            profile_focus="queue benchmark hotspots",
            benchmark_command=context["benchmark_command"],
            modality=context["modality"],
            domain_profiler=_domain_section(domain, "profiler", context),
            runtime_notes=context["runtime_notes"],
            profile_execution=context["profile_execution"],
            objective=context["objective"],
            profiler_support_name="neuron_profiler",
            profiler_mcp_name="vibesys-neuron-profiler",
            profiler_campaign_context="",
        ),
    }


@pytest.mark.parametrize("leak_check", DOMAIN_LEAK_CHECKS, ids=lambda check: check.source_domain)
def test_domain_specific_keywords_do_not_leak_to_vetted_domains(  # noqa: ANN201  # tracked: #288
    leak_check: DomainLeakCheck,
):
    failures: list[str] = []
    keywords = tuple((keyword, keyword.casefold()) for keyword in leak_check.keywords)

    for target_domain in leak_check.target_domains:
        prompts = _render_prompt_bundle(target_domain, modality=leak_check.modality)
        for prompt_name, rendered in prompts.items():
            rendered_folded = rendered.casefold()
            for keyword, keyword_folded in keywords:
                if keyword_folded in rendered_folded:
                    failures.append(f"{target_domain}/{prompt_name}: {keyword!r}")

    assert not failures, (
        f"{leak_check.source_domain} knowledge leaked into vetted prompts:\n" + "\n".join(failures)
    )


def test_profiler_prompts_calibrate_observer_effects():  # noqa: ANN201  # tracked: #288
    prompts = _render_prompt_bundle(DomainName.LLM_SERVING, modality="text_generation")

    for prompt_name in (
        "single_agent_nsys",
        "single_agent_torch",
        "profiler_nsys",
        "profiler_torch",
        "profiler_neuron",
    ):
        rendered = prompts[prompt_name]
        assert "observer_effect_fraction" in rendered
        assert "differ by more than 10%" in rendered
        assert "must not be converted into exclusive phase shares" in rendered


def test_microservice_otel_profiler_uses_critical_path_as_diagnostic_evidence():  # noqa: ANN201  # tracked: #288
    context = _NEUTRAL_CONTEXT
    rendered = render_template(
        "profilers/otel.j2",
        template_dir=_TEMPLATE_DIR,
        profile_focus="microservice latency",
        benchmark_command=context["benchmark_command"],
        domain_profiler=_domain_section(DomainName.MICROSERVICES, "profiler", context),
        runtime_notes=context["runtime_notes"],
        objective=context["objective"],
        profiler_support_name="otel_profiler",
        profiler_mcp_name="vibesys-otel-profiler",
        profiler_campaign_context="",
    )

    assert "trace_graphs()" in rendered
    assert "critical_path(path=..., telemetry_path=...)" in rendered
    assert "trace_breakdown(path=..., telemetry_path=...)" in rendered
    assert "representative waterfall" in rendered
    assert "--trace-graph-json" in rendered
    assert "async_relationships_excluded" in rendered
    assert "do not add overlapping sibling durations" in rendered
    assert "primary_value" in rendered
    assert "diagnostic evidence, not the scored result" in rendered
