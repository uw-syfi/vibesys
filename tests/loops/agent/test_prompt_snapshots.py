"""Snapshot tests for final rendered agent prompts.

These fixtures are intentionally plain text files. When prompt wording changes
on purpose, the fixture diff is the review artifact: it shows reviewers exactly
what an agent will see after all template includes and domain interpolation.

The domain under test is ``differential-dataflow`` (the in-place
superoptimization target); ``generic`` is the negative control that injects no
domain content of its own.
"""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from vibe_database.loops.agent.domain import render_domain_section, resolve_domain
from vibe_database.prompts import render_template

_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATE_DIR = _ROOT / "src" / "vibe_database" / "loops" / "agent" / "templates"
_SNAPSHOT_DIR = Path(__file__).with_name("fixtures") / "prompt_snapshots"

_DOMAIN = "differential-dataflow"
_ROLES = ("implementer", "judge", "single_agent", "orchestrator")

_BASE_CONTEXT = {
    "modality": "dataflow-opt",
    "reference_path": "/workspace/reference",
    "task": "TASK: micro-optimize the differential-dataflow bfs engine in place.",
    "pass_criteria": "PASS: output-equivalence with pristine round 0 and cpu_reduction_ratio > 1.0.",
    "objective": "OBJECTIVE: maximize cpu_reduction_ratio at byte-identical output.",
    "roadmap_text": "- major-1: todo - shave CPU on the bfs merge path at output parity.",
    "env_kind": "local",
}

_CONTEXTS = {
    "full": _BASE_CONTEXT
    | {
        "bench_path": "/workspace/bench",
        "accuracy_checker_path": "/workspace/acc_checker",
        "runtime_notes": "Runtime note: local CPU workspace.",
    },
    "minimal": _BASE_CONTEXT
    | {
        "bench_path": None,
        "accuracy_checker_path": None,
        "runtime_notes": "",
    },
}


def _domain_context(context: dict[str, object]) -> dict[str, object]:
    return {
        "modality": context["modality"],
        "reference_path": context["reference_path"],
        "bench_path": context["bench_path"],
        "accuracy_checker_path": context["accuracy_checker_path"],
        "runtime_notes": context["runtime_notes"],
    }


def _domain_section(domain: str, role: str, context: dict[str, object]) -> str:
    return render_domain_section(resolve_domain(domain), role, **_domain_context(context))


def _render_prompt(domain: str, role: str, context: dict[str, object]) -> str:
    if role == "implementer":
        return render_template(
            "implementer_prompt.j2",
            template_dir=_TEMPLATE_DIR,
            modality=context["modality"],
            reference_path=context["reference_path"],
            runtime_notes=context["runtime_notes"],
            task=context["task"],
            pass_criteria=context["pass_criteria"],
            feedback=None,
            domain_implementer=_domain_section(domain, "implementer", context),
        )
    if role == "judge":
        return render_template(
            "judge_prompt.j2",
            template_dir=_TEMPLATE_DIR,
            modality=context["modality"],
            objective=context["objective"],
            pass_criteria=context["pass_criteria"],
            runtime_notes=context["runtime_notes"],
            bench_path=context["bench_path"],
            accuracy_checker_path=context["accuracy_checker_path"],
            domain_judge=_domain_section(domain, "judge", context),
        )
    if role == "single_agent":
        return render_template(
            "single_agent_round_prompt.j2",
            template_dir=_TEMPLATE_DIR,
            modality=context["modality"],
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
            profiler_kind="native",
            profile_focus="",
            domain_single_agent=_domain_section(domain, "single_agent", context),
        )
    if role == "orchestrator":
        return render_template(
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
        )
    raise AssertionError(f"unknown prompt role: {role}")


def _snapshot_path(domain: str, case_name: str, role: str) -> Path:
    return _SNAPSHOT_DIR / domain / case_name / f"{role}.md"


def _assert_matches_snapshot(domain: str, case_name: str, role: str, rendered: str) -> None:
    snapshot = _snapshot_path(domain, case_name, role)
    expected = snapshot.read_text()
    if rendered == expected:
        return

    rendered_name = Path("rendered") / domain / case_name / f"{role}.md"
    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=str(snapshot),
            tofile=str(rendered_name),
        )
    )
    pytest.fail(f"Rendered prompt changed: {snapshot}\n{diff}")


@pytest.mark.parametrize("case_name,context", _CONTEXTS.items())
@pytest.mark.parametrize("role", _ROLES)
def test_differential_dataflow_prompt_snapshot(
    case_name: str, context: dict[str, object], role: str
):
    rendered = _render_prompt(_DOMAIN, role, context)
    _assert_matches_snapshot(_DOMAIN, case_name, role, rendered)


def test_rendered_prompts_keep_required_domain_content():
    context = _CONTEXTS["full"]
    prompts = {role: _render_prompt(_DOMAIN, role, context) for role in _ROLES}

    assert "differential-dataflow" in prompts["implementer"]
    assert "micro-optimization" in prompts["implementer"].lower()
    assert "byte-identical" in prompts["implementer"]
    assert "output-equivalence" in prompts["judge"].lower()
    assert "No reward hacking" in prompts["judge"]
    assert "micro-optimization" in prompts["single_agent"].lower()
    assert "output stays byte-identical and CPU follows" in prompts["orchestrator"]


def test_minimal_prompt_omits_optional_checker_paths():
    context = _CONTEXTS["minimal"]
    judge = _render_prompt(_DOMAIN, "judge", context)
    single_agent = _render_prompt(_DOMAIN, "single_agent", context)

    assert "/workspace/bench/benchmark.py" not in judge
    assert "/workspace/acc_checker/equivalence_gate.py" not in judge
    assert "/workspace/bench/benchmark.py" not in single_agent
    assert "/workspace/acc_checker/equivalence_gate.py" not in single_agent


def test_generic_prompts_do_not_receive_domain_content():
    context = _CONTEXTS["full"]
    prompts = {role: _render_prompt("generic", role, context) for role in _ROLES}

    # Anchors chosen to be unique to the domain role sections — NOT the shared
    # dataflow-opt modality fragment, which mentions differential-dataflow
    # regardless of --domain.
    assert "You earn the win" not in prompts["implementer"]
    assert "truth to regenerate here" not in prompts["judge"]
    assert "output stays byte-identical and CPU follows" not in prompts["orchestrator"]
