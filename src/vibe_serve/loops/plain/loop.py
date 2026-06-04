"""Issue-tracker driven loop.

Outer flow per iteration:
  1. Drain all OPEN issues: pick next → fresh implementer → fresh judge → close on PASS
     or leave open with feedback on FAIL. Issues exhausting their attempt budget are
     marked BLOCKED and skipped.
  2. Once the queue is drained, run the perf evaluator. The perf evaluator may file
     up to ``max_issues_per_perf_eval`` new issues via the create_issue tool, capped
     server-side.
  3. Loop back to step 1 with the new issues.

The very first iteration auto-creates one bootstrap FEATURE issue describing the
LLM serving build task (rendered from ``templates/bootstrap_issue.j2``), so the
implementer phase always has something to chew on.

State machine: ``PlainLoopState`` (in ``state.json``) tracks only the cursor —
which iteration we're in, which issue is currently being processed, and what
phase we're in. The store (in ``issues.json``) is the source of truth for which
issues exist.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from vibe_serve.config import Config, as_config
from vibe_serve.constants import ComputeBackend, DEFAULT_COMPUTE_BACKEND
from vibe_serve.context import _RunContext
from vibe_serve.loops.plain.render import render_all
from vibe_serve.loops.plain.runner_ext import PlainLoopAgentRunner
from vibe_serve.loops.plain.issue_board import (
    Issue,
    IssueStatus,
    IssueBoard,
    IssueType,
)
from vibe_serve.prompts import Prompt
from vibe_serve.schemas import (
    IssueImplementerResponse,
    IssueJudgeResponse,
    IssuePerfEvalResponse,
    PerfMetrics,
    PerfTrend,
    Verdict,
)
from vibe_serve.sandbox.run_environment import (
    RunEnvironmentSpec,
    make_run_environment_spec,
)

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_STATE_VERSION = 1


# ---------------------------------------------------------------------------
# Loop state checkpoint (state.json)
# ---------------------------------------------------------------------------


@dataclass
class PlainLoopState:
    """Serialisable cursor for the issue loop.

    The store (issues.json) is the source of truth for which issues exist;
    this state only records *where* the loop was when it last paused.
    """

    version: int = _STATE_VERSION
    round_idx: int = 0  # 0-indexed outer round
    phase: str = "implementer"  # "implementer" | "judge" | "perf_eval"
    current_issue_id: int | None = None
    bootstrap_done: bool = False


def _save_state(log_dir: Path, state: PlainLoopState) -> None:
    target = log_dir / "state.json"
    tmp = log_dir / "state.json.tmp"
    tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    os.replace(tmp, target)


def _load_state(log_dir: Path) -> PlainLoopState | None:
    path = log_dir / "state.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != _STATE_VERSION:
            return None
        return PlainLoopState(
            **{k: v for k, v in data.items() if k in PlainLoopState.__dataclass_fields__}
        )
    except (json.JSONDecodeError, TypeError):
        return None


def _determine_resume_point(
    state: PlainLoopState | None, store: IssueBoard
) -> tuple[int, str, int | None]:
    """Return ``(iteration, phase, current_issue_id)`` to resume from.

    *iteration* is 0-indexed.
    """
    if state is None:
        return 0, "implementer", None

    # Mid-judge crash: re-run the judge for the same issue
    if state.phase == "judge" and state.current_issue_id is not None:
        issue = store.get(state.current_issue_id)
        if issue is not None and issue.status in (IssueStatus.IN_PROGRESS, IssueStatus.OPEN):
            return state.round_idx, "judge", state.current_issue_id

    # Mid-implementer crash: re-run the implementer for the same issue
    if state.phase == "implementer" and state.current_issue_id is not None:
        issue = store.get(state.current_issue_id)
        if issue is not None and issue.status in (IssueStatus.IN_PROGRESS, IssueStatus.OPEN):
            return state.round_idx, "implementer", state.current_issue_id

    # Otherwise: drain remaining open issues, then fall through to perf_eval.
    # _determine_resume_point never returns "perf_eval" — the drain loop in
    # run_plain_loop short-circuits to perf_eval naturally when next_open()
    # returns None.
    return state.round_idx, "implementer", None


# ---------------------------------------------------------------------------
# Progress markdown helpers
# ---------------------------------------------------------------------------


def _init_progress(log_dir: Path) -> Path:
    progress_path = log_dir / "progress.md"
    if not progress_path.exists():
        progress_path.write_text("# Experiment Progress\n\n")
    return progress_path


def _update_progress_from_implementer(
    progress_path: Path,
    iteration: int,
    issue: Issue,
    response: IssueImplementerResponse,
) -> None:
    with progress_path.open("a", encoding="utf-8") as f:
        f.write(f"## Iter {iteration} — Implementer on issue #{issue.id}\n\n")
        f.write(f"**Issue**: [{issue.type.value}] {issue.title}\n\n")
        f.write(f"**Summary**: {response.summary}\n\n")
        if response.files_touched:
            f.write("**Files touched**:\n")
            for fp in response.files_touched:
                f.write(f"- `{fp}`\n")
            f.write("\n")
        f.write(f"**Self-check**: {response.self_check}\n\n")


def _update_progress_from_judge(
    progress_path: Path,
    iteration: int,
    issue: Issue,
    response: IssueJudgeResponse,
) -> None:
    with progress_path.open("a", encoding="utf-8") as f:
        f.write(f"### Iter {iteration} — Judge on issue #{issue.id}\n\n")
        f.write(f"**Verdict**: {response.verdict.value.upper()}\n\n")
        f.write(f"**Analysis**: {response.analysis}\n\n")
        if response.feedback:
            f.write(f"**Feedback**: {response.feedback}\n\n")
        if response.new_issues_filed:
            ids = ", ".join(f"#{i}" for i in response.new_issues_filed)
            f.write(f"**New issues filed**: {ids}\n\n")


def _update_progress_from_perf_eval(
    progress_path: Path,
    iteration: int,
    response: IssuePerfEvalResponse,
) -> None:
    with progress_path.open("a", encoding="utf-8") as f:
        f.write(f"## Iter {iteration} — Performance Evaluator\n\n")
        f.write(f"**Throughput trend**: {response.throughput_trend.value.upper()}\n\n")
        f.write(f"**Latency trend**: {response.latency_trend.value.upper()}\n\n")
        f.write(f"**Analysis**: {response.analysis}\n\n")
        if response.new_issue_ids:
            ids = ", ".join(f"#{i}" for i in response.new_issue_ids)
            f.write(f"**New issues filed**: {ids}\n\n")
        if response.evaluator_feedback:
            f.write("**Notes for next perf evaluator**:\n")
            for note in response.evaluator_feedback:
                f.write(f"- {note}\n")
            f.write("\n")


def _save_perf_metrics(
    perf_metrics_path: Path, iteration: int, response: IssuePerfEvalResponse
) -> None:
    existing = json.loads(perf_metrics_path.read_text(encoding="utf-8"))
    entry = {
        "iteration": iteration,
        "timestamp": datetime.now().isoformat(),
        "throughput_trend": response.throughput_trend.value,
        "latency_trend": response.latency_trend.value,
        "metrics": response.metrics.model_dump(),
        "new_issue_ids": response.new_issue_ids,
    }
    existing.append(entry)
    perf_metrics_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    per_iter = perf_metrics_path.parent / f"iter-{iteration}-perf.json"
    per_iter.write_text(json.dumps(entry, indent=2), encoding="utf-8")


def _init_perf_metrics(log_dir: Path) -> Path:
    perf_dir = log_dir / "perf"
    perf_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = perf_dir / "metrics.json"
    if not metrics_path.exists():
        metrics_path.write_text("[]", encoding="utf-8")
    return metrics_path


# ---------------------------------------------------------------------------
# Workspace sync
# ---------------------------------------------------------------------------


def _sync_workspace_files(
    progress_path: Path,
    perf_metrics_path: Path,
    issues_dir: Path,
    workspace: Path,
) -> None:
    """Mirror progress.md, perf_metrics.json, and the per-issue markdown
    directory into the agent workspace.

    NOTE: progress.md and the per-issue ``issues/`` directory are mirrored
    for HUMAN inspection only — agents do not consult them during reasoning.
    The implementer has no tracker tools (the relevant issue is inlined
    into its system prompt), and the judge/perf_eval read the canonical
    store via MCP tools (CLI backend) or LangChain tools (deepagents
    backend), both of which call ``store.reload()`` before each access.
    perf_metrics.json IS consulted directly by the perf_eval agent template
    so the evaluator can compare current results against the best-performing
    historical iteration.

    The canonical ``issues.json`` lives in the workspace root so the MCP
    server, the loop's ``IssueBoard``, and a human inspecting the workspace
    all see the same file. We do not copy it here.
    """
    shutil.copy2(progress_path, workspace / "progress.md")
    shutil.copy2(perf_metrics_path, workspace / "perf_metrics.json")
    if issues_dir.is_dir():
        shutil.copytree(issues_dir, workspace / "issues", dirs_exist_ok=True)


# ---------------------------------------------------------------------------
# Implementer retry context
# ---------------------------------------------------------------------------


def _latest_judge_review(issue: Issue) -> dict | None:
    """Return the most recent judge FAIL review on this issue, or ``None``.

    Walks ``issue.history`` in reverse looking for a status-transition
    event whose ``actor`` is ``"judge"``. Such events only happen on a
    judge verdict (PASS → CLOSED, FAIL → OPEN); since the implementer is
    only invoked while the issue is OPEN/IN_PROGRESS, any judge event in
    history must have been a FAIL — the corresponding feedback is what we
    want to surface to the next implementer attempt.

    Prefers the structured ``payload`` (added in the per-issue MD feature)
    but falls back to the truncated ``note`` for backwards compatibility
    with pre-payload runs. Returns ``None`` if there's no prior judge
    review or both feedback/analysis are empty.
    """
    for evt in reversed(issue.history):
        if evt.actor != "judge" or "->" not in evt.action:
            continue
        payload = evt.payload or {}
        feedback = (payload.get("feedback") or evt.note or "").strip()
        analysis = (payload.get("analysis") or "").strip()
        if not feedback and not analysis:
            return None
        return {
            "feedback": feedback,
            "analysis": analysis,
            "iteration": evt.iteration,
        }
    return None


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _ensure_bootstrap_issue(
    store: IssueBoard,
    *,
    state: PlainLoopState,
    log_dir: Path,
    ctx: _RunContext,
    prompt: Prompt,
) -> None:
    """Auto-create the initial feature issue on the first run.

    Idempotent on resume — checks state.bootstrap_done first.
    """
    if state.bootstrap_done:
        return
    description = prompt.render(
        "bootstrap_issue.j2",
        reference_path=ctx.ref_name,
        acc_checker_path=ctx.judge_acc_checker_path,
        bench_path=ctx.judge_bench_path,
        runtime_notes=ctx.run_environment_view.prompt_notes,
    )
    issue = store.create(
        type=IssueType.FEATURE,
        title="Build FastAPI inference server for the reference model",
        description=description,
        created_by="loop:bootstrap",
        iteration=max(state.round_idx + 1, 1),
    )
    state.bootstrap_done = True
    _save_state(log_dir, state)
    ctx.lprint(f"[bootstrap] created initial issue #{issue.id}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_plain_loop(
    config: Config,
    exp_name: str,
    reference_path: str,
    *,
    max_rounds: int = 5,
    max_attempts_per_issue: int = 3,
    max_issues_per_perf_eval: int = 3,
    existing: bool = False,
    resume_state: PlainLoopState | None = None,
    debug: bool = False,
    acc_checker: str | None = None,
    bench: str | None = None,
    nsys_profiler: str | None = None,
    skills_dirs: list[str] | None = None,
    run_environment: RunEnvironmentSpec | None = None,
    agent_backend: str | None = None,
    cli_provider: str | None = None,
    backend: ComputeBackend = DEFAULT_COMPUTE_BACKEND,
) -> bool:
    """Run the issue-tracker driven loop.

    Returns ``True`` if the loop terminates with no remaining open issues
    (everything resolved). Returns ``False`` if the iteration budget is
    exhausted with open work remaining, or if the run gets stuck (every
    remaining issue is BLOCKED).
    """
    config = as_config(config)
    # Issue loop always uses git tracking so judge test scripts and the
    # issue tracker mirror persist across iterations.
    git_tracking = True
    run_environment = run_environment or make_run_environment_spec()

    with _RunContext(
        config=config,
        exp_name=exp_name,
        reference_path=reference_path,
        existing=existing,
        debug=debug,
        acc_checker=acc_checker,
        bench=bench,
        nsys_profiler=nsys_profiler,
        skills_dirs=skills_dirs,
        run_environment=run_environment,
        git_tracking=git_tracking,
        agent_backend=agent_backend,
        cli_provider=cli_provider,
        backend=backend,
    ) as ctx:
        ctx.lprint(f"[log] experiment log: {ctx.run_log_path}")
        ctx.lprint(f"[log] experiment root: {ctx.exp_dir}")
        ctx.lprint(f"[log] model: {ctx.model_name}")
        prompt = Prompt(_TEMPLATE_DIR, ctx.backend)
        # ---- Initialize state, store, and progress files ----
        progress_path = _init_progress(ctx.log_dir)
        perf_metrics_path = _init_perf_metrics(ctx.log_dir)
        issues_dir = ctx.log_dir / "issues"

        # The canonical issues.json lives inside the unified workspace so
        # the MCP server (spawned by Claude Code with cwd=workspace) and the
        # loop's IssueBoard both read/write the same file. A convenience
        # symlink at log_dir/issues.json keeps the historical layout for
        # vibeserve-shell users and tooling that walks logs/.
        store_path = ctx.workspace / "issues.json"
        log_link = ctx.log_dir / "issues.json"
        if not log_link.exists() and not log_link.is_symlink():
            try:
                log_link.symlink_to(os.path.relpath(store_path, ctx.log_dir))
            except OSError:
                pass  # symlink is convenience-only

        # Wire the per-issue markdown renderer as a store on_change hook
        # so every successful save (including tool-created issues from
        # judge/perf_eval) re-renders the human-readable mirror.
        # Forward-declare `store` so the lambda's late binding resolves.
        store: IssueBoard  # type: ignore[no-redef]
        store = IssueBoard(
            store_path,
            on_change=lambda: render_all(issues_dir, store),
        )
        # Initial render covers the case where no mutation happens before
        # the first _sync_workspace_files call (e.g. on resume with no
        # bootstrap and no immediate work).
        render_all(issues_dir, store)

        # Wrap the runner so judge/perf_eval invokes auto-receive issue
        # tracker access (in-process @tool callables under deepagents,
        # MCP server spec under cli). The wrapper consumes an extra
        # ``iteration=`` kwarg on invoke() that the loop passes per call.
        # See vibe_serve/plain/runner_ext.py.
        ctx.agent_runner = PlainLoopAgentRunner(
            ctx.agent_runner,
            store=store,
            max_issues_per_perf_eval=max_issues_per_perf_eval,
        )

        state = resume_state if resume_state is not None else PlainLoopState()
        _ensure_bootstrap_issue(
            store, state=state, log_dir=ctx.log_dir, ctx=ctx, prompt=prompt,
        )

        # On resume, give every previously BLOCKED issue a fresh attempt
        # budget. The common reason a user resumes is that the prior run
        # bailed out because every remaining issue was blocked; without
        # this reset the resumed run would simply bail out again on the
        # first drain pass.
        if resume_state is not None:
            reopened = store.reopen_blocked(
                actor="loop:resume",
                iteration=max(state.round_idx + 1, 1),
                note="retried on resume",
            )
            if reopened:
                ids = ", ".join(f"#{i}" for i in reopened)
                ctx.lprint(
                    f"[resume] reopened {len(reopened)} previously blocked "
                    f"issue(s) for retry: {ids}"
                )

        # Determine where to resume from
        i, next_phase, pending_issue_id = _determine_resume_point(state, store)
        end_iteration = i + max_rounds

        if resume_state is not None:
            ctx.lprint(
                f"Resuming at iteration {i + 1} phase '{next_phase}'"
                + (f" issue #{pending_issue_id}" if pending_issue_id else "")
                + f", running up to {max_rounds} more rounds"
            )

        load_levels = config.perf_eval.load_levels
        previous_evaluator_feedback: list[str] | None = None

        while i < end_iteration:
            iter_label = i + 1
            ctx.lprint(f"\n{'='*60}")
            ctx.lprint(f"  Iteration {iter_label}/{end_iteration}")
            ctx.lprint(f"{'='*60}\n")

            # ---------------------------------------------------------------
            # DRAIN open issues
            # ---------------------------------------------------------------
            while True:
                # If we're resuming with a specific issue, pick that one first.
                if pending_issue_id is not None:
                    issue = store.get(pending_issue_id)
                    pending_issue_id = None
                else:
                    issue = store.next_open()

                if issue is None:
                    break

                if issue.attempts >= max_attempts_per_issue:
                    store.update_status(
                        issue.id,
                        IssueStatus.BLOCKED,
                        actor="loop",
                        iteration=iter_label,
                        note=f"exhausted {max_attempts_per_issue} attempts",
                    )
                    ctx.lprint(
                        f"[block] issue #{issue.id} blocked after {issue.attempts} attempts"
                    )
                    continue

                # Claim the issue
                if issue.status == IssueStatus.OPEN:
                    issue = store.update_status(
                        issue.id,
                        IssueStatus.IN_PROGRESS,
                        actor="loop",
                        iteration=iter_label,
                        note="claimed for processing",
                    )

                # ----- Implementer phase -----
                if next_phase != "judge":
                    state.round_idx = i
                    state.phase = "implementer"
                    state.current_issue_id = issue.id
                    _save_state(ctx.log_dir, state)

                    _sync_workspace_files(
                        progress_path, perf_metrics_path,
                        issues_dir, ctx.workspace,
                    )
                    ctx.reselect_gpu()

                    impl_system_prompt = prompt.render(
                        "implementer/system.j2",
                        reference_path=ctx.ref_name,
                        runtime_notes=ctx.run_environment_view.prompt_notes,
                        issue=issue,
                    )
                    impl_prompt = prompt.render(
                        "implementer/user.j2",
                        issue=issue,
                        prior_judge_review=_latest_judge_review(issue),
                    )

                    ctx.wait_for_debug(f"Implementer step on issue #{issue.id}")
                    ctx.lprint(f">>> Implementer working on issue #{issue.id}...")
                    # Implementer has no issue-tracker tools — the relevant
                    # issue is inlined into its system prompt — so no
                    # .mcp.json sandwich here.
                    issue_id_for_fallback = issue.id
                    impl_response = ctx.invoke(
                        kind="implementer",
                        system_prompt=impl_system_prompt,
                        user_prompt=impl_prompt,
                        response_cls=IssueImplementerResponse,
                        fallback_factory=lambda: IssueImplementerResponse(
                            issue_id=issue_id_for_fallback,
                            summary="Implementer did not produce a structured response.",
                            files_touched=[],
                            self_check="No structured response received.",
                        ),
                        round_label=f"impl issue #{issue.id} att{issue.attempts + 1}",
                    )

                    issue = store.increment_attempts(
                        issue.id,
                        actor="implementer",
                        iteration=iter_label,
                        note=impl_response.summary[:200],
                        payload=impl_response.model_dump(mode="json"),
                    )
                    _update_progress_from_implementer(
                        progress_path, iter_label, issue, impl_response
                    )
                    ctx.snapshot_workspace(
                        f"iter-{iter_label}-impl-{issue.id}-att{issue.attempts}"
                    )
                    ctx.lprint(
                        f"[snapshot] iter-{iter_label}-impl-{issue.id}-att{issue.attempts}"
                    )

                # next_phase only kicks in for the first issue we resume on
                next_phase = ""

                # ----- Judge phase -----
                state.round_idx = i
                state.phase = "judge"
                state.current_issue_id = issue.id
                _save_state(ctx.log_dir, state)

                _sync_workspace_files(
                    progress_path, perf_metrics_path,
                    issues_dir, ctx.workspace,
                )
                ctx.reselect_gpu()

                judge_system_prompt = prompt.render(
                    "judge/system.j2",
                    accuracy_checker_path=ctx.judge_acc_checker_path,
                    bench_path=ctx.judge_bench_path,
                    issue=issue,
                )
                judge_prompt = prompt.render("judge/user.j2", issue=issue)

                ctx.wait_for_debug(f"Judge step on issue #{issue.id}")
                ctx.lprint(f"\n>>> Judge reviewing issue #{issue.id}...")
                # PlainLoopAgentRunner injects tracker access (in-process
                # @tool callables under deepagents, MCPServerSpec under
                # cli) for kind="judge" — see
                # vibe_serve/plain/runner_ext.py. The judge may file
                # at most ONE bug-type issue per review; that policy is
                # enforced by the wrapper.
                judge_issue_id = issue.id
                judge_response = ctx.invoke(
                    kind="judge",
                    iteration=iter_label,
                    system_prompt=judge_system_prompt,
                    user_prompt=judge_prompt,
                    response_cls=IssueJudgeResponse,
                    fallback_factory=lambda: IssueJudgeResponse(
                        issue_id=judge_issue_id,
                        analysis="No structured response received from judge.",
                        feedback="Judge did not produce a structured response.",
                        verdict=Verdict.FAIL,
                        new_issues_filed=[],
                    ),
                    round_label=f"judge issue #{issue.id} att{issue.attempts}",
                )

                # Under cli the MCP server writes via a separate IssueBoard
                # on the same file, so reload picks up tool-created issues.
                # Under deepagents the @tool callables mutate the in-memory
                # store directly, so reload is a no-op there. reload() does
                # not fire on_change, so re-render explicitly to keep the
                # per-issue markdown view in sync.
                store.reload()
                render_all(issues_dir, store)

                _update_progress_from_judge(
                    progress_path, iter_label, issue, judge_response
                )
                ctx.snapshot_workspace(
                    f"iter-{iter_label}-judge-{issue.id}-att{issue.attempts}"
                )
                ctx.lprint(
                    f">>> Judge verdict on #{issue.id}: {judge_response.verdict.value.upper()}"
                )

                if judge_response.verdict == Verdict.PASS:
                    store.update_status(
                        issue.id,
                        IssueStatus.CLOSED,
                        actor="judge",
                        iteration=iter_label,
                        note=f"closed by judge after attempt {issue.attempts}",
                        payload=judge_response.model_dump(mode="json"),
                    )
                else:
                    store.update_status(
                        issue.id,
                        IssueStatus.OPEN,
                        actor="judge",
                        iteration=iter_label,
                        note=judge_response.feedback[:500],
                        payload=judge_response.model_dump(mode="json"),
                    )

                state.current_issue_id = None
                state.phase = "implementer"
                _save_state(ctx.log_dir, state)
                # Loop back to drain the next open issue.

            # ---------------------------------------------------------------
            # PERF_EVAL phase (after drain complete)
            # ---------------------------------------------------------------
            # Bail-out check: if every remaining issue is BLOCKED, we're stuck.
            remaining = [
                iss for iss in store.list()
                if iss.status not in (IssueStatus.CLOSED,)
            ]
            blocked_only = remaining and all(
                iss.status == IssueStatus.BLOCKED for iss in remaining
            )
            if blocked_only:
                ctx.lprint(
                    f"[stop] all remaining issues are blocked "
                    f"({len(remaining)} blocked); bailing out."
                )
                state.round_idx = i
                state.phase = "perf_eval"
                state.current_issue_id = None
                _save_state(ctx.log_dir, state)
                return False

            state.round_idx = i
            state.phase = "perf_eval"
            state.current_issue_id = None
            _save_state(ctx.log_dir, state)

            _sync_workspace_files(
                progress_path, perf_metrics_path,
                issues_dir, ctx.workspace,
            )
            ctx.reselect_gpu()

            perf_system_prompt = prompt.render(
                "perf_eval/system.j2",
                load_levels=load_levels,
                progress_path="progress.md",
                perf_metrics_path="perf_metrics.json",
                previous_evaluator_feedback=previous_evaluator_feedback,
                issue_create_cap=max_issues_per_perf_eval,
                runtime_notes=ctx.run_environment_view.prompt_notes,
            )
            perf_prompt = prompt.render("perf_eval/user.j2")

            ctx.wait_for_debug("Perf evaluator step")
            ctx.lprint("\n>>> Performance Evaluator benchmarking...")
            # PlainLoopAgentRunner injects tracker access for kind="perf_eval"
            # and scopes the per-iteration cap by the iteration kwarg below,
            # so issues filed here are counted against iter_label's budget.
            # See vibe_serve/plain/runner_ext.py.
            perf_response = ctx.invoke(
                kind="perf_eval",
                iteration=iter_label,
                system_prompt=perf_system_prompt,
                user_prompt=perf_prompt,
                response_cls=IssuePerfEvalResponse,
                fallback_factory=lambda: IssuePerfEvalResponse(
                    analysis="No structured response received from perf evaluator.",
                    metrics=PerfMetrics(load_levels=[]),
                    evaluator_feedback=[],
                    new_issue_ids=[],
                    throughput_trend=PerfTrend.MIXED,
                    latency_trend=PerfTrend.MIXED,
                ),
                round_label=f"perf_eval iter {iter_label}",
            )

            store.reload()
            render_all(issues_dir, store)

            _update_progress_from_perf_eval(progress_path, iter_label, perf_response)
            _save_perf_metrics(perf_metrics_path, iter_label, perf_response)
            ctx.snapshot_workspace(f"iter-{iter_label}-perf_eval")
            previous_evaluator_feedback = perf_response.evaluator_feedback or None

            ctx.lprint(
                f"\n>>> Perf trend: throughput={perf_response.throughput_trend.value.upper()}, "
                f"latency={perf_response.latency_trend.value.upper()}"
            )

            # Termination check: nothing open AND perf_eval filed nothing → done.
            still_open = store.list(status=IssueStatus.OPEN)
            if not still_open and not perf_response.new_issue_ids:
                ctx.lprint("[done] no open issues and perf_eval filed none.")
                state.round_idx = i + 1
                state.phase = "implementer"
                _save_state(ctx.log_dir, state)
                return True

            i += 1
            state.round_idx = i
            state.phase = "implementer"
            state.current_issue_id = None
            _save_state(ctx.log_dir, state)

        ctx.lprint("Run completed — iteration budget exhausted.")
        return False
