"""Reviewed snapshots of the final prompts emitted by the evolve plugin."""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from vibesys.constants import DomainName
from vibesys.domains.base import DomainRole
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_section
from vibesys.orchestration.evolve.models import (
    CandidateJudgeContext,
    CandidateProfilerContext,
    MutatorContext,
)
from vibesys.orchestration.evolve.population import Individual
from vibesys.orchestration.evolve.prompts import render_judge, render_mutator, render_profiler
from vibesys.orchestration.profilers import ProfilerKind, profiler_definition

_SNAPSHOT_DIR = Path(__file__).with_name("fixtures") / "prompt_snapshots"
_ROLES = ("mutator", "judge", "profiler")
_CRITERIA = "The candidate passes correctness and improves the headline metric."


@dataclass(frozen=True)
class _Case:
    domain: DomainName
    phase: str
    modality: str | None
    profiler: ProfilerKind
    objective: str
    accuracy_command: str
    benchmark_command: str


_CASES = (
    _Case(
        domain=DomainName.GENERIC,
        phase="cold-start",
        modality=None,
        profiler=ProfilerKind.LINUX_CPU,
        objective="Maximize total_ops_per_sec for the bounded SPSC queue.",
        accuracy_command="go run ./_evaluator/queue/cmd/accuracy --candidate ./queue-candidate.so",
        benchmark_command=(
            "go run ./_evaluator/queue/cmd/benchmark --candidate ./queue-candidate.so"
        ),
    ),
    _Case(
        domain=DomainName.GENERIC,
        phase="offspring",
        modality=None,
        profiler=ProfilerKind.LINUX_CPU,
        objective="Maximize total_ops_per_sec for the bounded SPSC queue.",
        accuracy_command="go run ./_evaluator/queue/cmd/accuracy --candidate ./queue-candidate.so",
        benchmark_command=(
            "go run ./_evaluator/queue/cmd/benchmark --candidate ./queue-candidate.so"
        ),
    ),
    _Case(
        domain=DomainName.LLM_SERVING,
        phase="cold-start",
        modality="text_generation",
        profiler=ProfilerKind.NSYS,
        objective="Maximize median_tok_per_sec for the local causal-LM server.",
        accuracy_command="uv run python accuracy_checker/checker.py",
        benchmark_command="uv run python benchmark/benchmark.py",
    ),
    _Case(
        domain=DomainName.LLM_SERVING,
        phase="offspring",
        modality="text_generation",
        profiler=ProfilerKind.NSYS,
        objective="Maximize median_tok_per_sec for the local causal-LM server.",
        accuracy_command="uv run python accuracy_checker/checker.py",
        benchmark_command="uv run python benchmark/benchmark.py",
    ),
)


def _domain_context(case: _Case) -> dict[str, object]:
    return {
        "modality": case.modality,
        "interface": "inprocess",
        "reference_path": "/workspace/reference",
        "benchmark_command": case.benchmark_command,
        "accuracy_command": case.accuracy_command,
        "runtime_notes": "Runtime note: local isolated workspace.",
        "profile_execution": "local",
        "workspace_sources": (),
    }


def _domain_section(case: _Case, role: DomainRole) -> str:
    return render_domain_section(resolve_domain(case.domain), role, **_domain_context(case))


def _parent() -> Individual:
    return Individual(
        id=7,
        generation=2,
        parent_id=3,
        inspiration_ids=(5,),
        commit="abc123",
        perf_metric=125.0,
        perf_unit="ops/s",
        metrics={"total_ops_per_sec": 125.0},
        passed=True,
        summary="Reduced synchronization overhead in the steady-state path.",
        feedback="All correctness gates passed.",
    )


def _inspiration() -> Individual:
    return Individual(
        id=5,
        generation=1,
        parent_id=1,
        commit="def456",
        perf_metric=118.0,
        perf_unit="ops/s",
        passed=True,
        summary="Separated producer and consumer hot metadata.",
    )


def _render_prompt(case: _Case, role: str) -> str:
    is_cold_start = case.phase == "cold-start"
    if role == "mutator":
        return render_mutator(
            MutatorContext(
                accuracy_command=case.accuracy_command,
                benchmark_command=case.benchmark_command,
                domain_implementer=_domain_section(case, DomainRole.IMPLEMENTER),
                failed_lessons=(
                    ["The prior candidate violated the documented ABI."] if is_cold_start else []
                ),
                inspirations=[] if is_cold_start else [_inspiration()],
                interface="inprocess",
                is_cold_start=is_cold_start,
                modality=case.modality,
                num_failed_attempts=1 if is_cold_start else 0,
                objective=case.objective,
                objectives=None,
                parent=None if is_cold_start else _parent(),
                reference_path="/workspace/reference",
                repair_seed=False,
                runtime_notes="Runtime note: local isolated workspace.",
            )
        )
    if role == "judge":
        return render_judge(
            CandidateJudgeContext(
                accuracy_command=case.accuracy_command,
                benchmark_command=case.benchmark_command,
                domain_judge=_domain_section(case, DomainRole.JUDGE),
                interface="inprocess",
                modality=case.modality,
                objective=case.objective,
                pass_criteria=_CRITERIA,
                runtime_notes="Runtime note: local isolated workspace.",
            )
        )
    if role == "profiler":
        definition = profiler_definition(case.profiler)
        return render_profiler(
            case.profiler.value,
            CandidateProfilerContext(
                benchmark_command=case.benchmark_command,
                domain_profiler=_domain_section(case, DomainRole.PROFILER),
                modality=case.modality,
                objective=case.objective,
                objectives=[],
                profile_execution="local",
                profile_focus=("Measure the headline metric and identify the dominant bottleneck."),
                profiler_mcp_name=definition.mcp_name,
                profiler_support_name=definition.support_name,
                runtime_notes="Runtime note: local isolated workspace.",
            ),
        )
    message = f"unknown prompt role: {role}"
    raise AssertionError(message)


def _snapshot_path(case: _Case, role: str) -> Path:
    return _SNAPSHOT_DIR / case.domain.value / case.phase / f"{role}.md"


def _assert_matches_snapshot(case: _Case, role: str, rendered: str) -> None:
    snapshot = _snapshot_path(case, role)
    if os.environ.get("UPDATE_PROMPT_SNAPSHOTS") == "1":
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(rendered, encoding="utf-8")
        return
    expected = snapshot.read_text(encoding="utf-8")
    if rendered == expected:
        return
    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=str(snapshot),
            tofile=str(Path("rendered") / case.domain.value / case.phase / f"{role}.md"),
        )
    )
    pytest.fail(f"Rendered prompt changed: {snapshot}\n{diff}")


@pytest.mark.parametrize("case", _CASES, ids=lambda case: f"{case.domain.value}-{case.phase}")
@pytest.mark.parametrize("role", _ROLES)
def test_evolve_prompt_snapshot(case: _Case, role: str) -> None:
    rendered = _render_prompt(case, role).rstrip() + "\n"
    _assert_matches_snapshot(case, role, rendered)
