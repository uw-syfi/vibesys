"""Orchestrator-driven build loop.

Replaces the curriculum loop with an *autonomous* flow: an Orchestrator
agent decides each round what the Implementer should build and what
pass criteria the Judge should enforce, optionally asking a Profiler to
collect kernel-level data first.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vibe_serve.config import Config
from vibe_serve.constants import ComputeBackend, DEFAULT_COMPUTE_BACKEND
from vibe_serve.context import _RunContext
from vibe_serve.loops.agent import issue_board
from vibe_serve.schemas import (
    OrchestratorPlan,
    PreRoundDecision,
    ProfilerSummary,
)
from vibe_serve.loops.profiler import invoke_profiler
from vibe_serve.prompts import render_template
from vibe_serve.schemas import (
    ImplementerResponse,
    JudgeResponse,
    Verdict,
)
from vibe_serve.sandbox.run_environment import (
    RunEnvironmentSpec,
    make_run_environment_spec,
)


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

    def to_json(self) -> dict:
        return {
            "round": self.round_number,
            "commit": self.commit,
            "perf_metric": self.perf_metric,
            "perf_unit": self.perf_unit,
            "passed": self.passed,
            "profile_skipped": self.profile_skipped,
        }

    @classmethod
    def from_json(cls, data: dict) -> "_RoundRecord":
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
    for r in records:
        if r.perf_metric is None or not r.passed:
            continue
        if best is None or r.perf_metric > best.perf_metric:
            best = r
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
    fresh = [
        r
        for r in records
        if r.perf_metric is not None and not r.profile_skipped
    ]
    if len(fresh) < min_streak:
        return None
    latest_unit = fresh[-1].perf_unit
    same_unit = [r for r in fresh if r.perf_unit == latest_unit]
    if len(same_unit) < min_streak:
        return None
    tail = same_unit[-min_streak:]
    perfs = [r.perf_metric for r in tail]
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
# Git helpers
# ---------------------------------------------------------------------------


def _current_commit_sha(ctx: _RunContext) -> str | None:
    if not ctx.git_tracking:
        return None
    try:
        result = ctx._git_run(["git", "rev-parse", "HEAD"], check=False)
        if result.returncode != 0:
            return None
        return result.stdout.decode(errors="replace").strip()
    except Exception:
        return None


def _git_checkout(ctx: _RunContext, sha: str) -> bool:
    """Check out *sha* into the working tree.

    Uses ``git checkout -- .`` style (non-branch) so subsequent commits
    continue to land on the current branch as new commits after the
    reverted state.
    """
    try:
        ctx._git_run(["git", "checkout", sha, "--", "."])
        return True
    except Exception as exc:
        ctx.lprint(f"[warn] git checkout {sha[:8]} failed: {exc}")
        return False


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
    ctx: _RunContext,
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
            need_profile=False, profile_focus="", reasoning="fallback: default to skip",
        ),
        round_label=f"round-{round_number}-pre",
    )
    issue_board.append_pre_round_decision(progress_path, round_number, decision)
    return decision


def _run_profiler(
    ctx: _RunContext,
    *,
    round_number: int,
    profile_focus: str,
    modality: str,
    progress_path: Path,
    objective: str,
) -> ProfilerSummary | None:
    template = (
        "profiler_prompt_torch.j2" if ctx.profiler_kind == "torch"
        else "profiler_prompt_nsys.j2"
    )
    system_prompt = render_template(
        template,
        template_dir=_TEMPLATE_DIR,
        profile_focus=profile_focus,
        bench_path=ctx.profiler_bench_path,
        modality=modality,
        runtime_notes=ctx.run_environment_view.prompt_notes,
        env_kind=ctx.run_environment_view.env_kind,
        objective=objective,
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


def _run_orchestrator_plan(
    ctx: _RunContext,
    *,
    round_number: int,
    objective: str,
    profiler_summary: ProfilerSummary | None,
    carry: _CarryOver,
    progress_path: Path,
    roadmap_text: str,
    plateau_warning: str | None,
) -> OrchestratorPlan:
    system_prompt = render_template(
        "orchestrator_plan_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        objective=objective,
        profiler_summary=profiler_summary,
        regression_info=carry.regression_info,
        exhaustion_info=carry.exhaustion_info,
        roadmap_text=roadmap_text,
        plateau_warning=plateau_warning,
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
    ctx: _RunContext,
    *,
    round_number: int,
    retry: int,
    plan: OrchestratorPlan,
    modality: str,
    feedback: str | None,
    progress_path: Path,
) -> ImplementerResponse:
    system_prompt = render_template(
        "implementer_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        reference_path=ctx.ref_name,
        modality=modality,
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
            "Carry out the orchestrator's task above. Append your summary "
            "to progress.md when done."
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
    ctx: _RunContext,
    *,
    round_number: int,
    retry: int,
    plan: OrchestratorPlan,
    modality: str,
    progress_path: Path,
    objective: str,
) -> JudgeResponse:
    system_prompt = render_template(
        "judge_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        accuracy_checker_path=ctx.judge_acc_checker_path,
        bench_path=ctx.judge_bench_path,
        pass_criteria=plan.pass_criteria,
        modality=modality,
        retry=retry,
        runtime_notes=ctx.run_environment_view.prompt_notes,
        env_kind=ctx.run_environment_view.env_kind,
        objective=objective,
    )
    response = ctx.invoke(
        kind="judge",
        system_prompt=system_prompt,
        user_prompt=(
            "Review the implementation per the criteria above. Return "
            "only the JSON verdict."
        ),
        response_cls=JudgeResponse,
        fallback_factory=lambda: JudgeResponse(
            analysis="Judge produced no structured response.",
            feedback="No structured response received.",
            verdict=Verdict.FAIL,
        ),
        round_label=f"round-{round_number}-retry-{retry}-judge",
    )
    issue_board.append_judge(progress_path, round_number, retry, response)
    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-judge")
    return response


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_agent_loop(
    config: Config,
    exp_name: str,
    reference_path: str,
    objective: str,
    *,
    max_rounds: int = 24,
    max_retries_per_round: int = 3,
    start_round: int = 1,
    existing: bool = False,
    debug: bool = False,
    acc_checker: str | None = None,
    bench: str | None = None,
    nsys_profiler: str | None = None,
    torch_profiler: str | None = None,
    profiler_kind: str = "auto",
    skills_dirs: list[str] | None = None,
    run_environment: RunEnvironmentSpec | None = None,
    agent_backend: str | None = None,
    cli_provider: str | None = None,
    backend: ComputeBackend = DEFAULT_COMPUTE_BACKEND,
    modality: str = "text_generation",
) -> bool:
    """Run the orchestrator-driven build loop.

    Returns True iff the orchestrator declared the objective met within
    ``max_rounds``.  Returns False when the round budget is exhausted.
    """
    run_environment = run_environment or make_run_environment_spec()
    ctx = _RunContext(
        config=config,
        exp_name=exp_name,
        reference_path=reference_path,
        existing=existing,
        debug=debug,
        acc_checker=acc_checker,
        bench=bench,
        nsys_profiler=nsys_profiler,
        torch_profiler=torch_profiler,
        profiler_kind=profiler_kind,
        skills_dirs=skills_dirs,
        run_environment=run_environment,
        git_tracking=True,
        agent_backend=agent_backend,
        cli_provider=cli_provider,
        backend=backend,
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

    try:
        while round_number <= max_rounds:
            ctx.switch_log_file(f"round{round_number:03d}")
            ctx.lprint(f"\n{'='*60}\n  Round {round_number}/{max_rounds}\n{'='*60}\n")

            # --- Pre-round decision (skip on fresh cold start) ---
            profiler_summary: ProfilerSummary | None = None
            if not _is_fresh_cold_start(round_number, records):
                pre = _run_pre_round_decision(
                    ctx,
                    round_number=round_number,
                    objective=objective,
                    carry=carry,
                    progress_path=progress_path,
                )
                # FORCE-PROFILE override: every non-cold-start round profiles,
                # ignoring orchestrator's need_profile decision. Revert this
                # block to restore orchestrator-decided profiling.
                profiler_summary = _run_profiler(
                    ctx,
                    round_number=round_number,
                    profile_focus=pre.profile_focus or "general latency hotspots on /v1/completions",
                    modality=modality,
                    progress_path=progress_path,
                    objective=objective,
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
                    _git_checkout(ctx, target.commit)
                    ctx.lprint(
                        f"Reverted workspace to round {plan.revert_to_round} "
                        f"({target.commit[:8]})."
                    )
                else:
                    ctx.lprint(
                        f"[warn] cannot revert: no commit recorded for round "
                        f"{plan.revert_to_round}"
                    )

            # --- Implementer / Judge retry loop ---
            feedback: str | None = None
            passed = False
            for retry in range(1, max_retries_per_round + 1):
                ctx.lprint(f"\n--- attempt {retry}/{max_retries_per_round} ---\n")
                ctx.reselect_gpu()
                _run_implementer(
                    ctx,
                    round_number=round_number,
                    retry=retry,
                    plan=plan,
                    modality=modality,
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
                    progress_path=progress_path,
                    objective=objective,
                )
                if verdict.verdict == Verdict.PASS:
                    passed = True
                    break
                feedback = verdict.feedback

            # --- Record round result & update carry-over ---
            commit = _current_commit_sha(ctx)
            # `profile_skipped` is True when no fresh profile ran this round
            # (cold-start or the orchestrator/framework decided to skip).
            # The plateau detector ignores skipped-profile rounds so cached
            # / inherited perf numbers don't masquerade as fresh measurements.
            profile_skipped = profiler_summary is None
            perf_metric = (
                profiler_summary.perf_metric
                if (profiler_summary and passed)
                else None
            )
            perf_unit = (
                profiler_summary.perf_unit
                if (profiler_summary and passed)
                else None
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

            if not passed:
                issue_board.append_exhaustion_note(
                    progress_path, round_number, max_retries_per_round, feedback or "",
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
                    if best is None or perf_metric > best.perf_metric:
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
