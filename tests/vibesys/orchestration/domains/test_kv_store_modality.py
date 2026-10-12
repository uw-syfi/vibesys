"""Tests for the kv_store modality templates and input bundle."""

from __future__ import annotations

from pathlib import Path

from tests.vibesys.domain_sections import EMPTY_DOMAIN_SECTIONS

import vibesys.orchestration.multi.core_policy.prompts as multi_templates
from entrypoints.cli import _MODALITIES
from vibesys.inputs import load_project_task
from vibesys.orchestration.multi.core_policy.api import run_prompt_variables
from vibesys.orchestration.multi.strategy.api import (
    GateFacts,
    HypothesisPlan,
    ImplementerClaims,
    JudgePrompt,
)
from vibesys.prompts import render_template
from vs_project.api import Project

_TEMPLATE_DIR = Path(multi_templates.__file__).parent
_PROJECT_ROOT = Path(__file__).resolve().parents[4]


def test_kv_store_is_a_registered_modality() -> None:
    assert "kv_store" in _MODALITIES


def test_kv_store_input_bundle_loads() -> None:
    project = Project.open(_PROJECT_ROOT / "examples" / "database" / "kv-store")
    bundle = load_project_task(project, project.select_task("default"))
    assert bundle.domain.value == "generic"
    assert bundle.benchmark_result is not None
    assert bundle.benchmark_result.metric == "throughput_ops_per_sec"


def test_kv_store_judge_prompt_mentions_resp2_not_http() -> None:
    criteria_text = "PC"
    context = JudgePrompt(
        round_number=1,
        retry=1,
        plan=HypothesisPlan(
            hypothesis_id="h1", hypothesis="claim", task="task", pass_criteria=criteria_text
        ),
        claims=ImplementerClaims(
            summary="s",
            expected_behavior="e",
            hypothesis_outcome="nominated",
            candidate_disposition="unassessed",
        ),
        framework=GateFacts(),
        modality="kv_store",
        interface="service",
    )
    variables = {name: getattr(context, name) for name in type(context).model_fields}
    variables.pop("template")
    output = render_template(
        "judge.j2",
        template_dir=_TEMPLATE_DIR,
        **run_prompt_variables(
            domain_sections=EMPTY_DOMAIN_SECTIONS,
            objective="OBJ",
            environment_notes="",
            agent_evaluation=False,
            validation_recipe_schema="{}",
        ),
        **variables,
    )
    assert "RESP2" in output
    assert "HTTP server" in output
    assert "VibeServeModel" not in output
    assert "p99" in output


def test_kv_store_linux_cpu_profiler_gets_resp2_specific_guidance() -> None:
    """Regression test: linux_cpu.j2 used to omit the {% include %} that pulls
    in _modality/kv_store/profiler.j2, so a kv_store profiling round silently
    lost the RESP2-specific py-spy/perf/strace guidance even though it's the
    modality this profiler kind is actually paired with in examples/database/kv-store.
    """
    output = render_template(
        "profilers/linux_cpu.j2",
        template_dir=_TEMPLATE_DIR,
        profile_focus="",
        benchmark_command="uv run python .vibesys/tasks/default/benchmark/benchmark.py",
        modality="kv_store",
        domain_profiler="",
        runtime_notes="",
        objective="OBJ",
        profiler_support_name="linux_cpu_profiler",
        profiler_mcp_name="vibesys-linux-cpu-profiler",
        campaign=None,
    )
    assert "RESP2" in output
    assert "py-spy record" in output
    assert "uv run python .vibesys/tasks/default/benchmark/benchmark.py --port 6380" in output


def test_kv_store_implementer_prompt_points_at_workspace_reference() -> None:
    output = render_template(
        "_modality/kv_store/implementer.j2",
        template_dir=_TEMPLATE_DIR,
        reference_path=".vibesys/tasks/default/reference/seed_server.py",
    )
    assert "`.vibesys/tasks/default/reference/seed_server.py` is a reference baseline" in output
