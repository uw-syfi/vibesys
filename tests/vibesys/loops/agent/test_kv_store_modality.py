"""Tests for the kv_store modality templates and input bundle."""

from __future__ import annotations

from pathlib import Path

from entrypoints.headless import _MODALITIES
from vibesys.input_manifest import load_project_task
from vibesys.prompts import PROMPTS_DIR, render_template
from vs_project import Project

_TEMPLATE_DIR = PROMPTS_DIR / "loops" / "agent"
_PROJECT_ROOT = Path(__file__).resolve().parents[4]


def test_kv_store_is_a_registered_modality():  # noqa: ANN201  # tracked: #288
    assert "kv_store" in _MODALITIES


def test_kv_store_input_bundle_loads():  # noqa: ANN201  # tracked: #288
    project = Project.open(_PROJECT_ROOT / "examples" / "kv-store")
    bundle = load_project_task(project, project.select_task("default"))
    assert bundle.domain.value == "generic"
    assert bundle.benchmark_result is not None
    assert bundle.benchmark_result.metric == "throughput_ops_per_sec"


def test_kv_store_judge_prompt_mentions_resp2_not_http():  # noqa: ANN201  # tracked: #288
    output = render_template(
        "judge_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality="kv_store",
        interface="service",
        domain_judge="",
        accuracy_command="uv run python accuracy_checker/checker.py",
        benchmark_command="uv run python benchmark/benchmark.py",
        pass_criteria="PC",  # noqa: S106  # tracked: #288
        retry=1,
        runtime_notes="",
        profile_execution="local",
        objective="OBJ",
        pareto_archive_conflict=None,
    )
    assert "RESP2" in output
    assert "HTTP server" in output
    assert "VibeServeModel" not in output
    assert "p99" in output


def test_kv_store_linux_cpu_profiler_gets_resp2_specific_guidance():  # noqa: ANN201  # tracked: #288
    """Regression test: linux_cpu.j2 used to omit the {% include %} that pulls
    in _modality/kv_store/profiler.j2, so a kv_store profiling round silently
    lost the RESP2-specific py-spy/perf/strace guidance even though it's the
    modality this profiler kind is actually paired with in examples/kv-store.
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
    )
    assert "RESP2" in output
    assert "py-spy record" in output
    assert "uv run python .vibesys/tasks/default/benchmark/benchmark.py --port 6380" in output


def test_kv_store_implementer_prompt_points_at_workspace_reference():  # noqa: ANN201  # tracked: #288
    output = render_template(
        "_modality/kv_store/implementer.j2",
        template_dir=_TEMPLATE_DIR,
        reference_path=".vibesys/tasks/default/reference/seed_server.py",
    )
    assert "`.vibesys/tasks/default/reference/seed_server.py` is a reference baseline" in output
