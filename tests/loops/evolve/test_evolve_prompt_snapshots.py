"""Snapshot tests for fully rendered evolutionary-loop prompts.

The fixtures are review artifacts: intentional prompt changes should update the
plain Markdown snapshots so reviewers can inspect exactly what each role sees.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

import pytest

from vibesys.domains.base import DomainName, DomainRole
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_section
from vibesys.loops.evolve.loop import _render
from vibesys.loops.evolve.population import Individual
from vibesys.profilers import ProfilerKind, profiler_definition

_SNAPSHOT_DIR = Path(__file__).with_name("fixtures") / "prompt_snapshots"
_ROLES = ("mutator", "judge", "profiler")


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
        benchmark_command="go run ./_evaluator/queue/cmd/benchmark --candidate ./queue-candidate.so",
    ),
    _Case(
        domain=DomainName.GENERIC,
        phase="offspring",
        modality=None,
        profiler=ProfilerKind.LINUX_CPU,
        objective="Maximize total_ops_per_sec for the bounded SPSC queue.",
        accuracy_command="go run ./_evaluator/queue/cmd/accuracy --candidate ./queue-candidate.so",
        benchmark_command="go run ./_evaluator/queue/cmd/benchmark --candidate ./queue-candidate.so",
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
    }


def _domain_section(case: _Case, role: DomainRole) -> str:
    return render_domain_section(resolve_domain(case.domain), role, **_domain_context(case))


def _parent() -> Individual:
    return Individual(
        id=7,
        generation=2,
        parent_id=3,
        inspiration_ids=[5],
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
    common = {
        "modality": case.modality,
        "interface": "inprocess",
        "runtime_notes": "Runtime note: local isolated workspace.",
        "env_kind": "local",
        "objective": case.objective,
        "accuracy_command": case.accuracy_command,
        "benchmark_command": case.benchmark_command,
    }
    if role == "mutator":
        return _render(
            "mutator_prompt.j2",
            **common,
            reference_path="/workspace/reference",
            domain_implementer=_domain_section(case, DomainRole.IMPLEMENTER),
            parent=None if is_cold_start else _parent(),
            inspirations=[] if is_cold_start else [_inspiration()],
            is_cold_start=is_cold_start,
            objectives=None,
            failed_lessons=(
                ["The prior candidate violated the documented ABI."] if is_cold_start else []
            ),
            num_failed_attempts=1 if is_cold_start else 0,
            repair_seed=False,
        )
    if role == "judge":
        return _render(
            "judge_prompt.j2",
            **common,
            domain_judge=_domain_section(case, DomainRole.JUDGE),
            pass_criteria="The candidate passes correctness and improves the headline metric.",
        )
    if role == "profiler":
        definition = profiler_definition(case.profiler)
        return _render(
            definition.prompt_template,
            **common,
            domain_profiler=_domain_section(case, DomainRole.PROFILER),
            profile_focus="Measure the headline metric and identify the dominant bottleneck.",
            profiler_support_name=definition.support_name,
            profiler_mcp_name=definition.mcp_name,
        )
    raise AssertionError(f"unknown prompt role: {role}")


def _snapshot_path(case: _Case, role: str) -> Path:
    return _SNAPSHOT_DIR / case.domain.value / case.phase / f"{role}.md"


def _assert_matches_snapshot(case: _Case, role: str, rendered: str) -> None:
    snapshot = _snapshot_path(case, role)
    expected = snapshot.read_text()
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
    _assert_matches_snapshot(case, role, _render_prompt(case, role))
