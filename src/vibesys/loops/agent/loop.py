"""Orchestrator-driven build loop.

Replaces the curriculum loop with an *autonomous* flow: an Orchestrator
agent decides each round what the Implementer should build and what
pass criteria the Judge should enforce, optionally asking a Profiler to
collect kernel-level data first.
"""

from __future__ import annotations

import json
import math
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vibesys.agents.progress import RoundProgress
from vibesys.config import Config
from vibesys.constants import DEFAULT_COMPUTE_BACKEND, ComputeBackend
from vibesys.context import create_run_context
from vibesys.domains.base import DomainDefinition, DomainName, DomainRole
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_section
from vibesys.input_manifest import BenchmarkResult
from vibesys.loops.agent import issue_board
from vibesys.loops.profiler import invoke_profiler
from vibesys.profilers import (
    ProfilerKind,
    profiler_definition,
    require_profiler_kind,
)
from vibesys.prompts import render_template
from vibesys.run import LoopContext
from vibesys.sandbox.run_environment import (
    RunEnvironmentSpec,
    make_run_environment_spec,
)
from vibesys.schemas import (
    ImplementerResponse,
    JudgeResponse,
    OrchestratorPlan,
    PreRoundDecision,
    ProfilerSummary,
    SingleAgentRoundResponse,
    Verdict,
)
from vibesys.server.events import (
    BenchmarkResultData,
    EventStatus,
    EventType,
    JudgeResultData,
    RoundFinishedData,
    SubprocessOutputData,
)

# Candidate process boundaries selected by ``--interface``. Language, tooling,
# and artifact requirements belong to the selected domain and input bundle.
_INTERFACES = ("inprocess", "service")
DEFAULT_INTERFACE = "inprocess"

_INNER_LOOPS = ("multi-agent", "single-agent")

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


# ---------------------------------------------------------------------------
# Rounds state (persisted to log_dir/rounds.json)
# ---------------------------------------------------------------------------


@dataclass
class _RoundRecord:
    round_number: int
    commit: str | None
    perf_metric: float | None
    perf_unit: str | None
    passed: bool
    # True when the orchestrator chose to skip profiling this round; the
    # perf_metric (if any) was reused / inherited from a prior measurement
    # rather than freshly measured this round.  Plateau detection ignores
    # these so a chain of skipped-profile rounds doesn't masquerade as a
    # real plateau.
    profile_skipped: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "round": self.round_number,
            "commit": self.commit,
            "perf_metric": self.perf_metric,
            "perf_unit": self.perf_unit,
            "passed": self.passed,
            "profile_skipped": self.profile_skipped,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> _RoundRecord:
        return cls(
            round_number=int(data["round"]),
            commit=data.get("commit"),
            perf_metric=data.get("perf_metric"),
            perf_unit=data.get("perf_unit"),
            passed=bool(data.get("passed", False)),
            profile_skipped=bool(data.get("profile_skipped", False)),
        )


def _load_rounds_state(path: Path) -> list[_RoundRecord]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [_RoundRecord.from_json(d) for d in data]


def _save_rounds_state(path: Path, records: list[_RoundRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([r.to_json() for r in records], indent=2))


def _best_round(records: list[_RoundRecord]) -> _RoundRecord | None:
    best: _RoundRecord | None = None
    best_metric = float("-inf")
    for r in records:
        metric = r.perf_metric
        if metric is None or not r.passed:
            continue
        if best is None or metric > best_metric:
            best = r
            best_metric = metric
    return best


# ---------------------------------------------------------------------------
# Plateau detection
# ---------------------------------------------------------------------------


_PLATEAU_THRESHOLD_PCT = 5.0
_PLATEAU_MIN_STREAK = 3


def _detect_plateau(
    records: list[_RoundRecord],
    *,
    threshold_pct: float = _PLATEAU_THRESHOLD_PCT,
    min_streak: int = _PLATEAU_MIN_STREAK,
) -> str | None:
    """Return a warning string if the most recent ``min_streak`` rounds
    with **fresh, same-unit** perf metrics stayed within ``threshold_pct``
    of each other; else None.

    Rules:
    - ``profile_skipped`` rounds don't count as fresh measurements (their
      perf was reused from earlier).
    - Only rounds with the *same* ``perf_unit`` as the latest fresh round
      count toward the streak — comparing latency_ms against tok/s as raw
      floats is a category error.
    - Failed rounds (``passed=False`` or no perf_metric) are stepped over.

    The orchestrator gets this verbatim in its prompt; phrasing is
    user-facing.
    """
    fresh = [r for r in records if r.perf_metric is not None and not r.profile_skipped]
    if len(fresh) < min_streak:
        return None
    latest_unit = fresh[-1].perf_unit
    same_unit = [r for r in fresh if r.perf_unit == latest_unit]
    if len(same_unit) < min_streak:
        return None
    tail = same_unit[-min_streak:]
    perfs = [r.perf_metric for r in tail if r.perf_metric is not None]
    hi = max(perfs)
    lo = min(perfs)
    if hi <= 0:
        return None
    spread_pct = (hi - lo) / hi * 100
    if spread_pct >= threshold_pct:
        return None
    unit_suffix = f" {latest_unit}" if latest_unit else ""
    rounds = [r.round_number for r in tail]
    return (
        f"The last {min_streak} rounds with a fresh perf measurement (rounds "
        f"{rounds[0]}–{rounds[-1]}) all landed in {lo:.2f}–{hi:.2f}{unit_suffix} "
        f"— a {spread_pct:.2f}% spread, well within bench noise. Whatever you've "
        f"been working on for those rounds is not actually moving the headline "
        f"metric."
    )


# ---------------------------------------------------------------------------
# Carry-over state between rounds
# ---------------------------------------------------------------------------


@dataclass
class _CarryOver:
    regression_info: str | None = None
    exhaustion_info: str | None = None


# ---------------------------------------------------------------------------
# Round phases
# ---------------------------------------------------------------------------


def _is_fresh_cold_start(round_number: int, records: list[_RoundRecord]) -> bool:
    """True for round 1 of a fresh run (no prior rounds recorded)."""
    return round_number == 1 and not records


def _run_pre_round_decision(
    ctx: LoopContext,
    *,
    round_number: int,
    objective: str,
    carry: _CarryOver,
    progress_path: Path,
) -> PreRoundDecision:
    system_prompt = render_template(
        "orchestrator_pre_round_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        objective=objective,
        regression_info=carry.regression_info,
        exhaustion_info=carry.exhaustion_info,
    )
    decision = ctx.invoke(
        kind="orchestrator",
        system_prompt=system_prompt,
        user_prompt=(
            "Decide whether a profiling pass is needed before planning "
            "this round. Return only the JSON object."
        ),
        response_cls=PreRoundDecision,
        fallback_factory=lambda: PreRoundDecision(
            need_profile=False,
            profile_focus="",
            reasoning="fallback: default to skip",
        ),
        round_label=f"round-{round_number}-pre",
    )
    issue_board.append_pre_round_decision(progress_path, round_number, decision)
    return decision


def _profiler_prompt_template(
    profiler_kind: ProfilerKind,
    interface: str,
    *,
    supports_torch_profiler: bool = False,
) -> str:
    """Pick a profiler prompt compatible with the boundary and domain."""
    return _effective_profiler_definition(
        profiler_kind,
        interface,
        supports_torch_profiler=supports_torch_profiler,
    ).prompt_template


def _effective_profiler_definition(
    profiler_kind: ProfilerKind,
    interface: str,
    *,
    supports_torch_profiler: bool = False,
):
    """Return the profiler declaration compatible with this agent boundary."""
    kind = require_profiler_kind(profiler_kind)
    if kind is ProfilerKind.NONE:
        raise ValueError("No profiler prompt exists when profiling is disabled.")
    definition = profiler_definition(kind)
    if definition.requires_inprocess and interface != "inprocess":
        definition = profiler_definition(ProfilerKind.NSYS)
    if definition.requires_domain_torch_support and not supports_torch_profiler:
        definition = profiler_definition(ProfilerKind.NSYS)
    return definition


def _run_profiler(
    ctx: LoopContext,
    *,
    round_number: int,
    profile_focus: str,
    modality: str | None,
    interface: str,
    domain_definition: DomainDefinition,
    progress_path: Path,
    objective: str,
) -> ProfilerSummary | None:
    template = _profiler_prompt_template(
        ctx.profiler_kind,
        interface,
        supports_torch_profiler=domain_definition.supports_torch_profiler,
    )
    domain_profiler = render_domain_section(
        domain_definition,
        DomainRole.PROFILER,
        **_domain_render_context(ctx, modality, interface),
    )
    system_prompt = render_template(
        template,
        template_dir=_TEMPLATE_DIR,
        profile_focus=profile_focus,
        benchmark_command=ctx.profiler_benchmark_command,
        modality=modality,
        domain_profiler=domain_profiler,
        runtime_notes=ctx.run_environment_view.prompt_notes,
        env_kind=ctx.run_environment_view.env_kind,
        objective=objective,
        profiler_support_name=profiler_definition(ctx.profiler_kind).support_name,
        profiler_mcp_name=profiler_definition(ctx.profiler_kind).mcp_name,
    )
    summary = invoke_profiler(
        ctx,
        system_prompt=system_prompt,
        round_label=f"round-{round_number}-profiler",
    )
    if summary is None:
        return None
    issue_board.append_profiler_summary(progress_path, round_number, summary)
    ctx.snapshot_workspace(f"round-{round_number}-profiler")
    return summary


def _domain_render_context(
    ctx: LoopContext, modality: str | None, interface: str
) -> dict[str, object]:
    """The uniform variable set every domain role file is rendered with.

    One context contract for all roles: a pack author can branch (``{% if … %}``)
    on any of these in any role file without memorizing which the loop happens
    to pass to which role. Variables that don't apply to the current run are
    falsy (``benchmark_command`` / ``accuracy_command`` when nothing is attached),
    so ``{% if benchmark_command %}`` works everywhere. ``interface`` lets a
    domain distinguish direct invocation from an over-the-wire service without
    treating that boundary as a language choice. See ``vibesys/domains/README.md``.
    """
    return {
        "modality": modality,
        "interface": interface,
        "reference_path": ctx.ref_name,
        "benchmark_command": ctx.judge_benchmark_command,
        "accuracy_command": ctx.judge_accuracy_command,
        "runtime_notes": ctx.run_environment_view.prompt_notes,
    }


def _run_orchestrator_plan(
    ctx: LoopContext,
    *,
    round_number: int,
    objective: str,
    profiler_summary: ProfilerSummary | None,
    carry: _CarryOver,
    progress_path: Path,
    roadmap_text: str,
    plateau_warning: str | None,
    modality: str | None,
    interface: str,
    domain_definition: DomainDefinition,
) -> OrchestratorPlan:
    domain_orchestrator = render_domain_section(
        domain_definition,
        DomainRole.ORCHESTRATOR,
        **_domain_render_context(ctx, modality, interface),
    )
    system_prompt = render_template(
        "orchestrator_plan_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        objective=objective,
        profiler_summary=profiler_summary,
        regression_info=carry.regression_info,
        exhaustion_info=carry.exhaustion_info,
        roadmap_text=roadmap_text,
        plateau_warning=plateau_warning,
        domain_orchestrator=domain_orchestrator,
        runtime_notes=ctx.run_environment_view.prompt_notes,
        env_kind=ctx.run_environment_view.env_kind,
    )
    plan = ctx.invoke(
        kind="orchestrator",
        system_prompt=system_prompt,
        user_prompt="Produce this round's plan. Return only the JSON object.",
        response_cls=OrchestratorPlan,
        fallback_factory=lambda: OrchestratorPlan(
            task="Re-check minimal server boots and /health returns 200.",
            pass_criteria="/health returns 200.",
            reasoning="fallback: orchestrator produced no structured response",
        ),
        round_label=f"round-{round_number}-plan",
    )
    issue_board.append_orchestrator_plan(progress_path, round_number, plan)
    return plan


def _run_implementer(
    ctx: LoopContext,
    *,
    round_number: int,
    retry: int,
    plan: OrchestratorPlan,
    modality: str | None,
    interface: str,
    domain_definition: DomainDefinition,
    feedback: str | None,
    progress_path: Path,
) -> ImplementerResponse:
    domain_implementer = render_domain_section(
        domain_definition,
        DomainRole.IMPLEMENTER,
        **_domain_render_context(ctx, modality, interface),
    )
    system_prompt = render_template(
        "implementer_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        reference_path=ctx.ref_name,
        modality=modality,
        interface=interface,
        domain_implementer=domain_implementer,
        task=plan.task,
        pass_criteria=plan.pass_criteria,
        retry=retry,
        feedback=feedback,
        runtime_notes=ctx.run_environment_view.prompt_notes,
        env_kind=ctx.run_environment_view.env_kind,
    )
    response = ctx.invoke(
        kind="implementer",
        system_prompt=system_prompt,
        user_prompt=(
            "Carry out the orchestrator's task above. Append your summary to progress.md when done."
        ),
        response_cls=ImplementerResponse,
        fallback_factory=lambda: ImplementerResponse(
            summary="Implementer produced no structured response.",
            expected_behavior="unknown",
        ),
        round_label=f"round-{round_number}-retry-{retry}-implementer",
    )
    issue_board.append_implementer(progress_path, round_number, retry, response)
    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-implementer")
    return response


def _run_judge(
    ctx: LoopContext,
    *,
    round_number: int,
    retry: int,
    plan: OrchestratorPlan,
    modality: str | None,
    interface: str,
    domain_definition: DomainDefinition,
    progress_path: Path,
    objective: str,
) -> JudgeResponse:
    domain_judge = render_domain_section(
        domain_definition,
        DomainRole.JUDGE,
        **_domain_render_context(ctx, modality, interface),
    )
    system_prompt = render_template(
        "judge_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        accuracy_command=ctx.judge_accuracy_command,
        benchmark_command=ctx.judge_benchmark_command,
        pass_criteria=plan.pass_criteria,
        modality=modality,
        interface=interface,
        domain_judge=domain_judge,
        retry=retry,
        runtime_notes=ctx.run_environment_view.prompt_notes,
        env_kind=ctx.run_environment_view.env_kind,
        objective=objective,
    )
    response = ctx.invoke(
        kind="judge",
        system_prompt=system_prompt,
        user_prompt=(
            "Review the implementation per the criteria above. Return only the JSON verdict."
        ),
        response_cls=JudgeResponse,
        fallback_factory=lambda: JudgeResponse(
            analysis="Judge produced no structured response.",
            feedback="No structured response received.",
            verdict=Verdict.FAIL,
        ),
        round_label=f"round-{round_number}-retry-{retry}-judge",
    )
    if ctx.supervisor is not None:
        ctx.supervisor.record(
            EventType.JUDGE_RESULT,
            status=(
                EventStatus.COMPLETED if response.verdict == Verdict.PASS else EventStatus.FAILED
            ),
            round_label=f"round-{round_number}-retry-{retry}",
            agent_kind="judge",
            data=JudgeResultData(
                verdict=response.verdict.value,
                feedback=response.feedback,
                attempt=retry,
            ),
        )
    issue_board.append_judge(progress_path, round_number, retry, response)
    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-judge")
    return response


def _run_single_agent_round(
    ctx: LoopContext,
    *,
    round_number: int,
    retry: int,
    plan: OrchestratorPlan,
    modality: str | None,
    interface: str,
    domain_definition: DomainDefinition,
    feedback: str | None,
    progress_path: Path,
    objective: str,
    profile_focus: str,
) -> SingleAgentRoundResponse:
    """Invoke one agent that plays implementer + judge + profiler.

    Used when ``--inner-loop=single-agent``. The same backend that the
    multi-agent loop hands to the implementer is used here — it has
    workspace write access plus shell access for benchmarks/profiling.
    """
    domain_single_agent = render_domain_section(
        domain_definition,
        DomainRole.SINGLE_AGENT,
        **_domain_render_context(ctx, modality, interface),
    )
    domain_profiler = render_domain_section(
        domain_definition,
        DomainRole.PROFILER,
        **_domain_render_context(ctx, modality, interface),
    )
    effective_profiler = (
        _effective_profiler_definition(
            ctx.profiler_kind,
            interface,
            supports_torch_profiler=domain_definition.supports_torch_profiler,
        )
        if ctx.profiler_kind is not ProfilerKind.NONE
        else None
    )
    system_prompt = render_template(
        "single_agent_round_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        reference_path=ctx.ref_name,
        modality=modality,
        interface=interface,
        domain_single_agent=domain_single_agent,
        domain_profiler=domain_profiler,
        task=plan.task,
        pass_criteria=plan.pass_criteria,
        retry=retry,
        feedback=feedback,
        objective=objective,
        profile_focus=profile_focus,
        profiler_kind=ctx.profiler_kind,
        profiler_support_name=(effective_profiler.support_name if effective_profiler else None),
        profiler_mcp_name=(effective_profiler.mcp_name if effective_profiler else None),
        supports_torch_profiler=domain_definition.supports_torch_profiler,
        benchmark_command=ctx.judge_benchmark_command,
        accuracy_command=ctx.judge_accuracy_command,
        runtime_notes=ctx.run_environment_view.prompt_notes,
        env_kind=ctx.run_environment_view.env_kind,
    )
    response = ctx.invoke(
        kind="implementer",
        system_prompt=system_prompt,
        user_prompt=(
            "Carry out the orchestrator's task above end-to-end "
            "(implement, self-judge, profile) and return only the JSON object."
        ),
        response_cls=SingleAgentRoundResponse,
        fallback_factory=lambda: SingleAgentRoundResponse(
            summary="Single-agent produced no structured response.",
            expected_behavior="unknown",
            self_review="No structured response received.",
            feedback="No structured response received.",
            verdict=Verdict.FAIL,
            bottlenecks="",
            suggestions="",
            profile_analysis="",
        ),
        round_label=f"round-{round_number}-retry-{retry}-single-agent",
    )
    issue_board.append_single_agent_round(progress_path, round_number, retry, response)
    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-single-agent")
    return response


def _profiler_summary_from_single_agent(
    response: SingleAgentRoundResponse,
) -> ProfilerSummary:
    """Adapt a single-agent response into a ProfilerSummary for the orchestrator."""
    return ProfilerSummary(
        analysis=response.profile_analysis,
        bottlenecks=response.bottlenecks,
        suggestions=response.suggestions,
        perf_metric=response.perf_metric,
        perf_unit=response.perf_unit,
    )


def _run_framework_accuracy_gate(
    ctx: LoopContext,
    *,
    round_number: int,
    retry: int,
    progress_path: Path,
) -> str | None:
    """Run the immutable manifest accuracy command after an agent reports PASS."""
    changed = ctx.trusted_input_changes()
    command = ctx.judge_accuracy_command
    if changed:
        feedback = "Evaluator-owned files were modified: " + ", ".join(changed)
        issue_board.append_framework_accuracy_gate(
            progress_path,
            round_number,
            retry,
            command=command or "(not configured)",
            passed=False,
            output=feedback,
        )
        ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-framework-accuracy")
        ctx.lprint(f"[framework-accuracy] FAIL: {feedback}")
        return feedback
    if not command:
        return None

    ctx.lprint(f"[framework-accuracy] running: {command}")
    try:
        result = ctx.judge_backend.execute(command)
        output = result.output.strip()
        passed = result.exit_code == 0
        _publish_subprocess_output(
            ctx,
            process_id=f"accuracy-{round_number}-{retry}",
            process_kind="accuracy_checker",
            content=result.output,
        )
    except Exception as exc:
        output = f"accuracy command could not be executed: {exc}"
        passed = False

    changed_after_execution = ctx.trusted_input_changes()
    if changed_after_execution:
        mutation = "Evaluator-owned files changed during accuracy execution: " + ", ".join(
            changed_after_execution
        )
        output = f"{output}\n{mutation}".strip()
        passed = False

    issue_board.append_framework_accuracy_gate(
        progress_path,
        round_number,
        retry,
        command=command,
        passed=passed,
        output=output[-8000:],
    )
    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-framework-accuracy")
    if passed:
        ctx.lprint("[framework-accuracy] PASS")
        return None

    feedback = f"Framework accuracy gate failed.\n{output[-4000:]}"
    ctx.lprint(f"[framework-accuracy] FAIL: {output[-1000:]}")
    return feedback


_FRAMEWORK_BENCHMARK_MARKER = "__VIBESYS_FRAMEWORK_BENCHMARK_JSON__"


def _metric_values(value: object, metric: str) -> list[object]:
    if isinstance(value, dict):
        matches = [item for key, item in value.items() if key == metric]
        for item in value.values():
            matches.extend(_metric_values(item, metric))
        return matches
    if isinstance(value, list):
        matches: list[object] = []
        for item in value:
            matches.extend(_metric_values(item, metric))
        return matches
    return []


def _run_framework_benchmark(
    ctx: LoopContext,
    *,
    result_spec: BenchmarkResult | None,
    round_number: int,
    retry: int,
    progress_path: Path,
) -> tuple[str | None, float | None]:
    """Run and parse an opt-in trusted benchmark result contract."""
    if result_spec is None:
        return None, None

    base_command = ctx.judge_benchmark_command
    if not base_command:
        return "Benchmark result contract is configured without a benchmark command.", None

    output_path = f"/tmp/vibesys-framework-benchmark-{round_number}-{retry}.json"
    command = (
        f"{base_command} {shlex.quote(result_spec.json_argument)} {shlex.quote(output_path)}"
        f" && printf '\\n{_FRAMEWORK_BENCHMARK_MARKER}\\n'"
        f" && cat {shlex.quote(output_path)}"
    )
    ctx.lprint(f"[framework-benchmark] running: {base_command}")
    metric_value: float | None = None
    changed_before_execution = ctx.trusted_input_changes()
    if changed_before_execution:
        output = "Evaluator-owned files were modified: " + ", ".join(changed_before_execution)
        passed = False
    else:
        try:
            result = ctx.judge_backend.execute(command)
            output = result.output.strip()
            passed = result.exit_code == 0
            _publish_subprocess_output(
                ctx,
                process_id=f"benchmark-{round_number}-{retry}",
                process_kind="benchmark",
                content=result.output,
            )
        except Exception as exc:
            output = f"benchmark command could not be executed: {exc}"
            passed = False

    if passed:
        _, marker, encoded = output.rpartition(_FRAMEWORK_BENCHMARK_MARKER)
        if not marker:
            output = f"{output}\nbenchmark output did not include its result JSON".strip()
            passed = False
        else:
            try:
                payload = json.loads(encoded.strip())
                values = _metric_values(payload, result_spec.metric)
                if len(values) != 1:
                    raise ValueError(
                        f"expected exactly one {result_spec.metric!r} field, found {len(values)}"
                    )
                value = values[0]
                if isinstance(value, bool) or not isinstance(value, int | float):
                    raise ValueError(f"{result_spec.metric!r} is not numeric")
                metric_value = float(value)
                if not math.isfinite(metric_value):
                    raise ValueError(f"{result_spec.metric!r} is not finite")
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                output = f"{output}\ninvalid benchmark result: {exc}".strip()
                passed = False

    changed = [] if changed_before_execution else ctx.trusted_input_changes()
    if changed:
        output = (
            f"{output}\nEvaluator-owned files changed during benchmark execution: "
            + ", ".join(changed)
        ).strip()
        passed = False
        metric_value = None

    issue_board.append_framework_benchmark(
        progress_path,
        round_number,
        retry,
        command=base_command,
        passed=passed,
        metric_name=result_spec.metric,
        metric_value=metric_value,
        output=output[-8000:],
    )
    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-framework-benchmark")
    if passed:
        ctx.lprint(f"[framework-benchmark] PASS: {result_spec.metric}={metric_value}")
        if ctx.supervisor is not None and metric_value is not None:
            ctx.supervisor.record(
                EventType.BENCHMARK_RESULT,
                status=EventStatus.COMPLETED,
                round_label=f"round-{round_number}",
                data=BenchmarkResultData(
                    metric=result_spec.metric,
                    value=metric_value,
                    unit=result_spec.metric,
                ),
            )
        return None, metric_value

    feedback = f"Framework benchmark failed.\n{output[-4000:]}"
    ctx.lprint(f"[framework-benchmark] FAIL: {output[-1000:]}")
    return feedback, None


def _run_framework_gates(
    ctx: LoopContext,
    *,
    benchmark_result: BenchmarkResult | None,
    round_number: int,
    retry: int,
    progress_path: Path,
) -> tuple[str | None, float | None]:
    if ctx.agent_runner.backend_name == "stub":
        return None, None
    feedback = _run_framework_accuracy_gate(
        ctx,
        round_number=round_number,
        retry=retry,
        progress_path=progress_path,
    )
    if feedback is not None:
        return feedback, None
    return _run_framework_benchmark(
        ctx,
        result_spec=benchmark_result,
        round_number=round_number,
        retry=retry,
        progress_path=progress_path,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_agent_loop(
    config: Config,
    exp_name: str,
    input_path: str,
    accuracy_command: str,
    benchmark_command: str,
    objective: str,
    *,
    workspace_seed: Path | None = None,
    evaluator_path: Path | None = None,
    benchmark_result: BenchmarkResult | None = None,
    max_rounds: int = 24,
    max_retries_per_round: int = 3,
    start_round: int = 1,
    existing: bool = False,
    debug: bool = False,
    profiler_kind: ProfilerKind = ProfilerKind.AUTO,
    skills_dirs: list[str] | None = None,
    run_environment: RunEnvironmentSpec | None = None,
    agent_backend: str | None = None,
    cli_provider: str | None = None,
    backend: ComputeBackend = DEFAULT_COMPUTE_BACKEND,
    modality: str | None = None,
    inner_loop: str = "multi-agent",
    domain: DomainName | None = None,
    interface: str = DEFAULT_INTERFACE,
) -> bool:
    """Run the orchestrator-driven build loop.

    Returns True iff the orchestrator declared the objective met within
    ``max_rounds``.  Returns False when the round budget is exhausted.

    ``inner_loop`` selects how each round's implement/judge/profile work
    is dispatched:

    - ``"multi-agent"`` (default): three specialist agents — implementer,
      judge, profiler — invoked in sequence.
    - ``"single-agent"``: one agent does all three in a single
      invocation per retry. Pre-round decision and standalone profiler
      passes are skipped; the prior round's profile output is fed to the
      orchestrator as ``profiler_summary``.

    ``interface`` selects only the evaluator-to-candidate process boundary:

    - ``"inprocess"`` (default): evaluator-owned code invokes the candidate
      directly using the input-defined contract.
    - ``"service"``: evaluator-owned code communicates with a running service
      through its network interface.

    Language, tooling, and artifact requirements come from the domain and input
    bundle rather than the process-boundary mode.
    """
    if inner_loop not in _INNER_LOOPS:
        raise ValueError(
            f"Unknown inner_loop {inner_loop!r}; choose from {', '.join(_INNER_LOOPS)}"
        )
    if interface not in _INTERFACES:
        raise ValueError(f"Unknown interface {interface!r}; choose from {', '.join(_INTERFACES)}")
    if domain is None:
        raise ValueError("domain is required; declare [agent].domain in vibesys.input.toml")
    # Resolve the registered domain once (fail fast on an unknown name). The
    # per-role files carry language, tooling, and use-case-specific contracts.
    domain_definition = resolve_domain(domain)
    if modality is None and domain_definition.name is DomainName.LLM_SERVING:
        modality = "text_generation"
    run_environment = run_environment or make_run_environment_spec()
    ctx = create_run_context(
        config=config,
        exp_name=exp_name,
        input_path=input_path,
        accuracy_command=accuracy_command,
        benchmark_command=benchmark_command,
        workspace_seed=workspace_seed,
        evaluator_path=evaluator_path,
        existing=existing,
        debug=debug,
        profiler_kind=profiler_kind,
        profiler_domain=domain_definition.name,
        skills_dirs=skills_dirs,
        run_environment=run_environment,
        git_tracking=True,
        agent_backend=agent_backend,
        cli_provider=cli_provider,
        backend=backend,
        environment_hooks=domain_definition.environment_hooks,
    )
    ctx.lprint(f"[log] orchestrate run: {ctx.run_log_path}")
    ctx.lprint(f"[log] experiment root: {ctx.exp_dir}")
    ctx.lprint(f"[log] objective: {objective.splitlines()[0] if objective else '(empty)'}")

    progress_path = ctx.workspace / "progress.md"
    issue_board.ensure_progress_file(progress_path)

    roadmap_path = ctx.workspace / "roadmap.md"
    issue_board.ensure_roadmap_file(roadmap_path)

    rounds_state_path = ctx.log_dir / "rounds.json"
    records = _load_rounds_state(rounds_state_path)

    carry = _CarryOver()
    round_number = start_round

    # When inner_loop == "single-agent", we don't run a separate
    # pre-round decision or profiler invocation. We thread the previous
    # round's combined response into the orchestrator's next plan as a
    # synthesized ProfilerSummary, and remember its profile focus across
    # rounds. The first round has no prior profile to feed forward.
    last_single_agent_response: SingleAgentRoundResponse | None = None
    last_profile_focus: str = "general latency hotspots on /v1/completions"

    try:
        while round_number <= max_rounds:
            ctx.switch_log_file(f"round{round_number:03d}")
            round_progress = RoundProgress(round_number, max_rounds)
            ctx.lprint(f"\n{'=' * 60}\n  {round_progress.label()}\n{'=' * 60}\n")

            with ctx.progress(round_progress):
                # --- Pre-round decision (skip on fresh cold start) ---
                profiler_summary: ProfilerSummary | None = None
                pre_decision: PreRoundDecision | None = None
                if inner_loop == "multi-agent":
                    if not _is_fresh_cold_start(round_number, records):
                        pre_decision = _run_pre_round_decision(
                            ctx,
                            round_number=round_number,
                            objective=objective,
                            carry=carry,
                            progress_path=progress_path,
                        )
                        if pre_decision.need_profile and ctx.profiler_kind is not ProfilerKind.NONE:
                            profiler_summary = _run_profiler(
                                ctx,
                                round_number=round_number,
                                profile_focus=pre_decision.profile_focus
                                or "general steady-state benchmark hotspots",
                                modality=modality,
                                interface=interface,
                                domain_definition=domain_definition,
                                progress_path=progress_path,
                                objective=objective,
                            )
                else:
                    # single-agent: feed the previous round's profile into the
                    # orchestrator as ProfilerSummary so it has a bottleneck signal.
                    if last_single_agent_response is not None:
                        profiler_summary = _profiler_summary_from_single_agent(
                            last_single_agent_response
                        )

                # --- Orchestrator plan ---
                roadmap_text = issue_board.read_roadmap(roadmap_path)
                plateau_warning = _detect_plateau(records)
                plan = _run_orchestrator_plan(
                    ctx,
                    round_number=round_number,
                    objective=objective,
                    profiler_summary=profiler_summary,
                    carry=carry,
                    progress_path=progress_path,
                    roadmap_text=roadmap_text,
                    plateau_warning=plateau_warning,
                    modality=modality,
                    interface=interface,
                    domain_definition=domain_definition,
                )

                # No early stop: the loop always consumes the full max_rounds
                # budget. Previously OrchestratorPlan had a ``done`` field that
                # could halt the loop; it was removed because the orchestrator
                # can't reliably tell when the objective is "fully met" and
                # early-stopping masks further optimization opportunities.

                # --- Optional rollback ---
                if plan.revert_to_round is not None:
                    target = next(
                        (r for r in records if r.round_number == plan.revert_to_round),
                        None,
                    )
                    if target and target.commit:
                        # Non-branch checkout so subsequent commits continue
                        # to land on the current branch as new commits after
                        # the reverted state.
                        ctx.git.checkout_tree(target.commit)
                        ctx.lprint(
                            f"Reverted workspace to round {plan.revert_to_round} ({target.commit[:8]})."
                        )
                    else:
                        ctx.lprint(
                            f"[warn] cannot revert: no commit recorded for round {plan.revert_to_round}"
                        )

                # --- Implementer / Judge retry loop ---
                feedback: str | None = None
                passed = False
                single_agent_response: SingleAgentRoundResponse | None = None
                framework_perf_metric: float | None = None
                for retry in range(1, max_retries_per_round + 1):
                    ctx.lprint(f"\n--- attempt {retry}/{max_retries_per_round} ---\n")
                    if inner_loop == "multi-agent":
                        ctx.reselect_gpu()
                        _run_implementer(
                            ctx,
                            round_number=round_number,
                            retry=retry,
                            plan=plan,
                            modality=modality,
                            interface=interface,
                            domain_definition=domain_definition,
                            feedback=feedback,
                            progress_path=progress_path,
                        )
                        ctx.reselect_gpu()
                        verdict = _run_judge(
                            ctx,
                            round_number=round_number,
                            retry=retry,
                            plan=plan,
                            modality=modality,
                            interface=interface,
                            domain_definition=domain_definition,
                            progress_path=progress_path,
                            objective=objective,
                        )
                        if verdict.verdict == Verdict.PASS:
                            gate_feedback, framework_perf_metric = _run_framework_gates(
                                ctx,
                                benchmark_result=benchmark_result,
                                round_number=round_number,
                                retry=retry,
                                progress_path=progress_path,
                            )
                            if gate_feedback is None:
                                passed = True
                                break
                            feedback = gate_feedback
                            continue
                        feedback = verdict.feedback
                    else:
                        ctx.reselect_gpu()
                        single_agent_response = _run_single_agent_round(
                            ctx,
                            round_number=round_number,
                            retry=retry,
                            plan=plan,
                            modality=modality,
                            interface=interface,
                            domain_definition=domain_definition,
                            feedback=feedback,
                            progress_path=progress_path,
                            objective=objective,
                            profile_focus=last_profile_focus,
                        )
                        if single_agent_response.verdict == Verdict.PASS:
                            gate_feedback, framework_perf_metric = _run_framework_gates(
                                ctx,
                                benchmark_result=benchmark_result,
                                round_number=round_number,
                                retry=retry,
                                progress_path=progress_path,
                            )
                            if gate_feedback is None:
                                passed = True
                                break
                            feedback = gate_feedback
                            continue
                        feedback = single_agent_response.feedback

                # --- Record round result & update carry-over ---
                commit = ctx.git.current_sha() if ctx.git_tracking else None
                # `profile_skipped` is True when no fresh profile ran this round
                # (cold-start or the orchestrator/framework decided to skip).
                # The plateau detector ignores skipped-profile rounds so cached
                # / inherited perf numbers don't masquerade as fresh measurements.
                #
                # For single-agent inner loop, `profiler_summary` carries the
                # PREVIOUS round's profile (fed forward to the orchestrator),
                # so this round's perf comes from `single_agent_response` instead.
                if inner_loop == "single-agent":
                    if single_agent_response is not None and framework_perf_metric is not None:
                        single_agent_response.perf_metric = framework_perf_metric
                        single_agent_response.perf_unit = (
                            benchmark_result.metric if benchmark_result else None
                        )
                    profile_skipped = single_agent_response is None or (
                        single_agent_response.perf_metric is None
                    )
                    perf_metric = (
                        single_agent_response.perf_metric
                        if (single_agent_response and passed)
                        else None
                    )
                    perf_unit = (
                        single_agent_response.perf_unit
                        if (single_agent_response and passed)
                        else None
                    )
                    # Remember the latest profile for the orchestrator's next plan
                    # and carry forward the implicit profile focus.
                    if single_agent_response is not None:
                        last_single_agent_response = single_agent_response
                else:
                    profile_skipped = profiler_summary is None and framework_perf_metric is None
                    if framework_perf_metric is not None and passed:
                        perf_metric = framework_perf_metric
                        perf_unit = benchmark_result.metric if benchmark_result else None
                    else:
                        perf_metric = (
                            profiler_summary.perf_metric if (profiler_summary and passed) else None
                        )
                        perf_unit = (
                            profiler_summary.perf_unit if (profiler_summary and passed) else None
                        )
                records.append(
                    _RoundRecord(
                        round_number=round_number,
                        commit=commit,
                        perf_metric=perf_metric,
                        perf_unit=perf_unit,
                        passed=passed,
                        profile_skipped=profile_skipped,
                    )
                )
                _save_rounds_state(rounds_state_path, records)
                if ctx.supervisor is not None:
                    ctx.supervisor.record(
                        EventType.ROUND_FINISHED,
                        status=EventStatus.COMPLETED if passed else EventStatus.FAILED,
                        round_label=f"round-{round_number}",
                        data=RoundFinishedData(
                            attempts=retry,  # pyright: ignore[reportPossiblyUnboundVariable]
                            judge_verdict="pass" if passed else "fail",
                            perf_metric=perf_metric,
                            perf_unit=perf_unit,
                        ),
                    )

                if not passed:
                    issue_board.append_exhaustion_note(
                        progress_path,
                        round_number,
                        max_retries_per_round,
                        feedback or "",
                    )
                    carry.exhaustion_info = (
                        f"Round {round_number} did not pass after "
                        f"{max_retries_per_round} attempts. Last judge feedback: "
                        f"{feedback or '(empty)'}"
                    )
                    carry.regression_info = None
                else:
                    carry.exhaustion_info = None
                    if perf_metric is not None:
                        best = _best_round(records[:-1])
                        # _best_round only returns rounds with a metric.
                        if (
                            best is None
                            or best.perf_metric is None
                            or perf_metric > best.perf_metric
                        ):
                            carry.regression_info = None
                        else:
                            carry.regression_info = (
                                f"Round {round_number} perf_metric="
                                f"{perf_metric}{(' ' + perf_unit) if perf_unit else ''} "
                                f"did not beat best={best.perf_metric}"
                                f"{(' ' + (best.perf_unit or '')) if best.perf_unit else ''} "
                                f"at round {best.round_number}."
                            )

                round_number += 1

        ctx.lprint(f"Reached max_rounds={max_rounds}. Stopping.")
        return True
    finally:
        ctx.close()


def _publish_subprocess_output(
    ctx: LoopContext,
    *,
    process_id: str,
    process_kind: str,
    content: str,
) -> None:
    if ctx.supervisor is None or not content:
        return
    ctx.supervisor.record(
        EventType.SUBPROCESS_OUTPUT,
        data=SubprocessOutputData(
            process_id=process_id,
            process_kind=process_kind,
            stream="stdout",
            content=content,
        ),
    )
