"""Tests for the kv_store modality templates and input bundle."""

from __future__ import annotations

from pathlib import Path

from vibe_sys.cli import _MODALITIES
from vibe_sys.input_manifest import load_input_bundle
from vibe_sys.prompts import render_template

_TEMPLATE_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "vibe_sys" / "loops" / "agent" / "templates"
)
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_kv_store_is_a_registered_modality():
    assert "kv_store" in _MODALITIES


def test_kv_store_input_bundle_loads():
    bundle = load_input_bundle(_PROJECT_ROOT / "examples" / "kv-store", project_root=_PROJECT_ROOT)
    assert bundle.domain.value == "generic"
    assert bundle.benchmark_result is not None
    assert bundle.benchmark_result.metric == "throughput_ops_per_sec"


def test_kv_store_judge_prompt_mentions_resp2_not_http():
    output = render_template(
        "judge_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality="kv_store",
        interface="service",
        domain_judge="",
        accuracy_command="uv run python accuracy_checker/checker.py",
        benchmark_command="uv run python benchmark/benchmark.py",
        pass_criteria="PC",
        retry=1,
        runtime_notes="",
        env_kind="local",
        objective="OBJ",
        accuracy_checker_path="accuracy_checker",
        bench_path="benchmark",
    )
    assert "RESP2" in output
    assert "HTTP server" in output
    assert "VibeServeModel" not in output
    assert "p99" in output
