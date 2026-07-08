"""Regression tests for keeping domain-specific prompt knowledge scoped.

The checks are data-driven on purpose: add one ``DomainLeakCheck`` when a domain
gets new distinctive terminology, or add target domains to vet more neutral
packs against an existing keyword set.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from vibe_serve.domains.base import DomainName
from vibe_serve.domains.registry import resolve_domain
from vibe_serve.domains.rendering import render_domain_section
from vibe_serve.profilers import ProfilerKind
from vibe_serve.prompts import render_template

_TEMPLATE_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "vibe_serve" / "loops" / "agent" / "templates"
)


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
            "modal_profile",
        ),
    ),
)

_NEUTRAL_CONTEXT: dict[str, object] = {
    "modality": None,
    "interface": "inprocess",
    "reference_path": "/workspace/reference/main.py",
    "bench_path": "/workspace/bench",
    "accuracy_checker_path": "/workspace/acc_checker",
    "runtime_notes": "",
    "task": "TASK: optimize a queue implementation for steady-state throughput.",
    "pass_criteria": "PASS: preserve FIFO behavior and improve the benchmark headline metric.",
    "objective": "OBJECTIVE: maximize operations per second for the queue benchmark.",
    "roadmap_text": "- major-1: todo - identify the next data-structure bottleneck.",
    "env_kind": "local",
}


def _domain_context(context: dict[str, object]) -> dict[str, object]:
    return {
        "modality": context["modality"],
        "interface": context["interface"],
        "reference_path": context["reference_path"],
        "bench_path": context["bench_path"],
        "accuracy_checker_path": context["accuracy_checker_path"],
        "runtime_notes": context["runtime_notes"],
    }


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
            bench_path=context["bench_path"],
            accuracy_checker_path=context["accuracy_checker_path"],
            domain_judge=_domain_section(domain, "judge", context),
        ),
        "single_agent_nsys": render_template(
            "single_agent_round_prompt.j2",
            template_dir=_TEMPLATE_DIR,
            modality=context["modality"],
            interface=context["interface"],
            env_kind=context["env_kind"],
            objective=context["objective"],
            runtime_notes=context["runtime_notes"],
            task=context["task"],
            pass_criteria=context["pass_criteria"],
            bench_path=context["bench_path"],
            accuracy_checker_path=context["accuracy_checker_path"],
            retry=1,
            feedback=None,
            reference_path=context["reference_path"],
            profiler_kind=ProfilerKind.NSYS,
            profile_focus="",
            domain_single_agent=_domain_section(domain, "single_agent", context),
            domain_profiler=_domain_section(domain, "profiler", context),
        ),
        "single_agent_torch": render_template(
            "single_agent_round_prompt.j2",
            template_dir=_TEMPLATE_DIR,
            modality=context["modality"],
            interface=context["interface"],
            env_kind=context["env_kind"],
            objective=context["objective"],
            runtime_notes=context["runtime_notes"],
            task=context["task"],
            pass_criteria=context["pass_criteria"],
            bench_path=context["bench_path"],
            accuracy_checker_path=context["accuracy_checker_path"],
            retry=1,
            feedback=None,
            reference_path=context["reference_path"],
            profiler_kind=ProfilerKind.TORCH,
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
            env_kind=context["env_kind"],
            domain_orchestrator=_domain_section(domain, "orchestrator", context),
        ),
        "profiler_nsys": render_template(
            "profiler_prompt_nsys.j2",
            template_dir=_TEMPLATE_DIR,
            profile_focus="queue benchmark hotspots",
            bench_path=context["bench_path"],
            modality=context["modality"],
            domain_profiler=_domain_section(domain, "profiler", context),
            runtime_notes=context["runtime_notes"],
            env_kind=context["env_kind"],
            objective=context["objective"],
        ),
        "profiler_torch": render_template(
            "profiler_prompt_torch.j2",
            template_dir=_TEMPLATE_DIR,
            profile_focus="queue benchmark hotspots",
            bench_path=context["bench_path"],
            modality=context["modality"],
            domain_profiler=_domain_section(domain, "profiler", context),
            runtime_notes=context["runtime_notes"],
            env_kind=context["env_kind"],
            objective=context["objective"],
        ),
        "profiler_neuron": render_template(
            "profiler_prompt_neuron.j2",
            template_dir=_TEMPLATE_DIR,
            profile_focus="queue benchmark hotspots",
            bench_path=context["bench_path"],
            modality=context["modality"],
            domain_profiler=_domain_section(domain, "profiler", context),
            runtime_notes=context["runtime_notes"],
            env_kind=context["env_kind"],
            objective=context["objective"],
        ),
    }


@pytest.mark.parametrize("leak_check", DOMAIN_LEAK_CHECKS, ids=lambda check: check.source_domain)
def test_domain_specific_keywords_do_not_leak_to_vetted_domains(
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
