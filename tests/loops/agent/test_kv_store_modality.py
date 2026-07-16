"""Tests for the kv_store modality templates and input bundle."""

from __future__ import annotations

from pathlib import Path

from vibe_serve.cli import _MODALITIES
from vibe_serve.input_manifest import load_input_bundle
from vibe_serve.prompts import render_template

_TEMPLATE_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "vibe_serve" / "loops" / "agent" / "templates"
)
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_kv_store_is_a_registered_modality():
    assert "kv_store" in _MODALITIES


def test_kv_store_input_bundle_loads():
    bundle = load_input_bundle(_PROJECT_ROOT / "examples" / "kv-store", project_root=_PROJECT_ROOT)
    assert bundle.domain.value == "generic"
    assert bundle.benchmark_result is not None
    assert bundle.benchmark_result.metric == "ops_per_cpu_sec"
    command = bundle.benchmark_command_display
    assert "--min-throughput-ops-per-sec 10000" in command
    assert "--max-read-p99-ms 1.0" in command
    assert "--saturation-probe-client-procs 8" in command


def test_kv_store_judge_prompt_uses_production_commands_and_score():
    accuracy = "uv run python accuracy_checker/checker.py"
    benchmark = (
        "uv run python benchmark/benchmark.py --client-procs 4 --min-throughput-ops-per-sec 10000"
    )
    output = render_template(
        "judge_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality="kv_store",
        interface="service",
        domain_judge="",
        accuracy_command=accuracy,
        benchmark_command=benchmark,
        pass_criteria="PC",
        retry=1,
        runtime_notes="",
        env_kind="local",
        objective="OBJ",
    )
    assert "RESP2" in output
    assert "HTTP server" in output
    assert "VibeServeModel" not in output
    assert "p99" in output
    assert accuracy in output
    assert benchmark in output
    assert "ops_per_cpu_sec" in output
    assert "python /checker.py" not in output
    assert "PERF_METRIC: <n> ops/sec" not in output


def test_kv_store_single_agent_includes_modality_contract():
    output = render_template(
        "single_agent_round_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality="kv_store",
        interface="service",
        env_kind="local",
        domain_single_agent="",
        domain_profiler="",
        task="TASK",
        pass_criteria="PC",
        retry=1,
        feedback=None,
        objective="Headline metric: ops_per_cpu_sec (maximize).",
        profile_focus="",
        profiler_kind="none",
        profiler_support_name=None,
        profiler_mcp_name=None,
        supports_torch_profiler=False,
        benchmark_command="uv run python benchmark/benchmark.py",
        accuracy_command="uv run python accuracy_checker/checker.py",
        reference_path="reference/seed_server.py",
        runtime_notes="",
    )
    assert "CANDIDATE_CONTRACT.md" in output
    assert "RESP2" in output
    assert "ops_per_cpu_sec" in output
    assert "canned replies" in output
