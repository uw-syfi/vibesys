"""Tests for the dataflow_opt modality, its bundle, schema additivity, and the
guarded ledger/focus prompt blocks.

``dataflow_opt`` is the in-place-superoptimization modality: two workspace
sources seed an editable ``engine/`` and a pristine ``_ref_engine/`` at the same
pinned commit, and a framework-owned bottleneck walk steers each round. Every
prompt addition is ``{% if %}``-guarded so a non-dataflow_opt run renders
identically.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from entrypoints.headless import _MODALITIES
from vibesys.input_manifest import load_input_bundle
from vibesys.prompts import PROMPTS_DIR, render_string, render_template
from vibesys.schemas import (
    OrchestratorPlan,
    PreRoundDecision,
    ProfilerSummary,
    RankedBottleneck,
)

_TEMPLATE_DIR = PROMPTS_DIR / "loops" / "agent"
_DOMAIN_DIR = PROMPTS_DIR / "domains" / "database"
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_BUNDLE = _PROJECT_ROOT / "examples" / "differential-dataflow-cpu-bench"

_PINNED_COMMIT = "4f05cbb61775a45844a0905de9dacfee1e91dd80"


# --------------------------------------------------------------------------- #
# Modality registration + bundle
# --------------------------------------------------------------------------- #


def test_dataflow_opt_is_a_registered_modality():  # noqa: ANN201  # tracked: #288
    assert "dataflow_opt" in _MODALITIES


def test_bundle_loads_database_domain_and_cpu_seconds_metric():  # noqa: ANN201  # tracked: #288
    bundle = load_input_bundle(_BUNDLE)
    assert bundle.domain.value == "database"
    assert bundle.benchmark_result is not None
    assert bundle.benchmark_result.metric == "cpu_seconds"


def test_bundle_declares_two_pinned_engine_trees():  # noqa: ANN201  # tracked: #288
    bundle = load_input_bundle(_BUNDLE)
    sources = {s.dest: s for s in bundle.workspace_sources}
    assert set(sources) == {"engine", "_ref_engine"}
    # Both trees pin the same repo + commit, so engine/ == _ref_engine/ byte for
    # byte at round 0 — the diff-discipline baseline is exact by construction.
    assert sources["engine"].commit == sources["_ref_engine"].commit == _PINNED_COMMIT
    assert sources["engine"].repo == sources["_ref_engine"].repo
    for src in sources.values():
        assert src.strip_git is True


def test_bundle_objectives_declare_cpu_seconds_min():  # noqa: ANN201  # tracked: #288
    raw = tomllib.loads((_BUNDLE / "objectives.toml").read_text())
    # A cpu_seconds objective with a minimize direction must be present somewhere
    # in the parsed manifest, regardless of how the schema nests it.
    flat = repr(raw)
    assert "cpu_seconds" in flat
    assert "min" in flat


# --------------------------------------------------------------------------- #
# Schema additivity (all additions default; old payloads still parse)
# --------------------------------------------------------------------------- #


def test_ranked_bottleneck_defaults():  # noqa: ANN201  # tracked: #288
    rb = RankedBottleneck(component="operators/reduce")
    assert rb.ir_pct == 0.0
    assert rb.top_functions == []


def test_profiler_summary_parses_without_ranked_bottlenecks():  # noqa: ANN201  # tracked: #288
    summary = ProfilerSummary(analysis="a", bottlenecks="b", suggestions="s")
    assert summary.ranked_bottlenecks == []


def test_pre_round_decision_defaults_active_component_empty():  # noqa: ANN201  # tracked: #288
    # The soft override lives on PreRoundDecision (the only consumed carrier);
    # it defaults empty so pre-existing payloads parse unchanged.
    decision = PreRoundDecision(need_profile=False, profile_focus="", reasoning="r")
    assert decision.active_component == ""
    # OrchestratorPlan must still parse an old payload with no new fields.
    OrchestratorPlan(task="t", pass_criteria="p", reasoning="r")  # noqa: S106


# --------------------------------------------------------------------------- #
# Modality prompt fragments
# --------------------------------------------------------------------------- #


def test_modality_judge_owns_diff_discipline_and_scores_cpu_seconds():  # noqa: ANN201  # tracked: #288
    out = render_template(
        "_modality/dataflow_opt/judge.j2",
        template_dir=_TEMPLATE_DIR,
        accuracy_command="uv run python accuracy_checker/checker.py",
        benchmark_command="uv run python benchmark/benchmark.py",
    )
    assert "diff -ru _ref_engine engine" in out
    assert "uv run python accuracy_checker/checker.py" in out
    assert "cpu_seconds" in out


def test_modality_implementer_names_engine_build_and_binary():  # noqa: ANN201  # tracked: #288
    out = render_template(
        "_modality/dataflow_opt/implementer.j2",
        template_dir=_TEMPLATE_DIR,
    )
    assert "cargo build --release --example bfs" in out
    assert "engine/target/release/examples/bfs" in out
    assert "_ref_engine" in out


# --------------------------------------------------------------------------- #
# Guarded ledger / focus blocks: present only when populated, absent otherwise
# --------------------------------------------------------------------------- #


def _impl_base() -> dict:
    return {
        "reference_path": "reference",
        "modality": "dataflow_opt",
        "interface": "direct",
        "domain_implementer": "",
        "task": "t",
        "pass_criteria": "p",
        "objective": "o",
        "objective_location": "OBJECTIVE.md",
        "plan_artifact_location": "x",
        "hypothesis_id": "h",
        "hypothesis": "",
        "activation_evidence": "",
        "falsification_criteria": "",
        "expected_effect": "",
        "minimum_acceptance_criteria": "",
        "invariants": "",
        "progress_location": "progress.md",
        "pareto_archive_location": "pf.md",
        "validation_location": "v",
        "validation_recipe_contract_location": "c",
        "retry": 1,
        "feedback": None,
        "continuation_step": None,
        "framework_revert_applied": False,
        "framework_revert_round": None,
        "framework_revert_commit": None,
        "gate_revalidation_pending": False,
        "gate_approved_perf_metric": None,
        "gate_approved_perf_unit": None,
        "gate_approved_evaluation_artifact": None,
        "runtime_notes": None,
        "framework_benchmark_enabled": False,
        "official_evaluation_due": False,
        "official_evaluation_reason": None,
        "recommended_skills": [],
        "prior_attempt_artifact_locations": (),
        "profile_execution": "local",
    }


def _orch_base() -> dict:
    return {
        "objective": "o",
        "objective_location": "OBJECTIVE.md",
        "profiler_summary": None,
        "regression_info": None,
        "exhaustion_info": None,
        "progress_location": "progress.md",
        "roadmap_location": "roadmap.md",
        "pareto_archive_location": "pf.md",
        "plateau_warning": None,
        "domain_orchestrator": "",
        "runtime_notes": None,
        "profile_execution": "local",
        "framework_benchmark_enabled": False,
        "official_eval_every": 3,
        "provisional_candidates": 0,
        "official_eval_cadence_due": False,
    }


def test_implementer_focus_block_only_when_active_component_set():  # noqa: ANN201  # tracked: #288
    with_focus = render_template(
        "implementer_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        active_component="operators/reduce",
        **_impl_base(),
    )
    assert "Focus this round" in with_focus
    assert "operators/reduce" in with_focus
    # Empty and absent both render without the block.
    assert "Focus this round" not in render_template(
        "implementer_prompt.j2", template_dir=_TEMPLATE_DIR, active_component="", **_impl_base()
    )
    assert "Focus this round" not in render_template(
        "implementer_prompt.j2", template_dir=_TEMPLATE_DIR, **_impl_base()
    )


def test_orchestrator_plan_ledger_blocks_only_when_populated():  # noqa: ANN201  # tracked: #288
    populated = render_template(
        "orchestrator_plan_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        active_component="operators/reduce",
        ledger_text="component | status",
        ranked_bottlenecks=[
            {"component": "operators/reduce", "ir_pct": 42.5, "top_functions": ["reduce::foo"]}
        ],
        **_orch_base(),
    )
    assert "Bottleneck walk" in populated
    assert "Ranked components" in populated
    assert "Ledger state" in populated
    empty = render_template(
        "orchestrator_plan_prompt.j2", template_dir=_TEMPLATE_DIR, **_orch_base()
    )
    assert "Bottleneck walk" not in empty
    assert "Ranked components" not in empty


def test_pre_round_soft_override_note_gated_on_walk_active():  # noqa: ANN201  # tracked: #288
    base = {
        "objective": "o",
        "objective_location": "OBJECTIVE.md",
        "regression_info": None,
        "exhaustion_info": None,
        "progress_location": "progress.md",
        "profiler_kind": "none",
        "profile_execution": "local",
        "has_history": True,
    }
    on = render_template(
        "orchestrator_pre_round_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        bottleneck_walk_active=True,
        **base,
    )
    assert "soft override" in on
    off = render_template("orchestrator_pre_round_prompt.j2", template_dir=_TEMPLATE_DIR, **base)
    assert "soft override" not in off


# --------------------------------------------------------------------------- #
# Domain-doc gating: dataflow_opt blocks appear only for that modality
# --------------------------------------------------------------------------- #


def _domain_ctx(modality: str | None) -> dict:
    return {
        "modality": modality,
        "interface": "direct",
        "reference_path": "reference",
        "benchmark_command": "b",
        "accuracy_command": "a",
        "runtime_notes": None,
        "profile_execution": "local",
        "workspace_sources": [],
    }


def test_database_domain_docs_gate_dataflow_opt_blocks():  # noqa: ANN201  # tracked: #288
    for name in ("implementer.md", "judge.md", "orchestrator.md"):
        raw = (_DOMAIN_DIR / name).read_text()
        dd = render_string(raw, **_domain_ctx("dataflow_opt"))
        kv = render_string(raw, **_domain_ctx("kv_store"))
        none = render_string(raw, **_domain_ctx(None))
        assert "dataflow_opt)" in dd, f"{name}: dataflow_opt block missing"
        assert "dataflow_opt)" not in kv, f"{name}: block leaked into kv_store"
        assert "dataflow_opt)" not in none, f"{name}: block leaked into None modality"
