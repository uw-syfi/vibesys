"""The agent loop's durable roadmap and progress memory.

The issue board supports two backward-compatible layouts:

  - ``roadmap.md`` + ``progress.md`` — the original compact layout.
  - ``roadmap/index.md`` + ``progress/round-NNNN.md`` — a layout that stays
    scannable when a run grows to hundreds of rounds.

Both surfaces together are this loop's planning artifact, parallel to
the plain loop's structured :class:`~vibesys.loops.plain.issue_board.IssueBoard`
(``issues.json``).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from vibesys.schemas import (
    FrameworkValidationResult,
    ImplementerResponse,
    JudgeResponse,
    OrchestratorPlan,
    PreRoundDecision,
    ProfilerSummary,
    SingleAgentRoundResponse,
    ValidationRecipeArtifact,
)

MEMORY_LAYOUTS = ("files", "directories")
#: Workspace-relative roots of the loop's durable memory, layout aside.
MEMORY_LAYOUTS_ROOTS = ("roadmap", "progress")
# The roadmap carries durable strategy, while progress files are an audit trail.
# Keep a bounded read helper for callers that explicitly request recent audit
# text. Agent prompts receive only the durable path and inspect it with tools.
_RECENT_PROGRESS_ROUNDS = 4


def resolve_paths(workspace: Path, layout: str) -> tuple[Path, Path]:
    """Resolve both memory locations, preserving the layout of resumed runs."""
    if layout not in MEMORY_LAYOUTS:
        raise ValueError(
            f"Unknown memory layout {layout!r}; choose from {', '.join(MEMORY_LAYOUTS)}"
        )

    def resolve(name: str) -> Path:
        legacy = workspace / f"{name}.md"
        directory = workspace / name
        if legacy.exists() and directory.exists():
            raise ValueError(
                f"Both {legacy.name} and {directory.name}/ exist; keep only one {name} layout"
            )
        if legacy.exists():
            return legacy
        if directory.exists():
            return directory
        return directory if layout == "directories" else legacy

    roadmap, progress = MEMORY_LAYOUTS_ROOTS
    return resolve(roadmap), resolve(progress)


def display_path(path: Path, workspace: Path) -> str:
    """Return an agent-facing workspace-relative memory location."""
    location = path.relative_to(workspace).as_posix()
    return f"{location}/" if path.is_dir() else location


def _structured_artifact_root(progress_path: Path) -> Path:
    """Return the framework-owned directory for typed role handoffs.

    Directory memory layouts keep the artifacts below ``progress/``.  Legacy
    ``progress.md`` runs use a sibling directory so the existing Markdown file
    remains untouched.
    """
    if progress_path.suffix == ".md":
        return progress_path.with_name(f"{progress_path.stem}-artifacts")
    return progress_path


def _write_json_atomic(path: Path, payload: object) -> Path:
    """Atomically replace a framework-owned JSON handoff artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def write_plan_artifact(progress_path: Path, round_number: int, plan: OrchestratorPlan) -> Path:
    """Persist the exact typed plan used by the framework for one round."""
    path = _structured_artifact_root(progress_path) / "plans" / f"round-{round_number:04d}.json"
    return _write_json_atomic(path, plan.model_dump(mode="json"))


#: Name tail of a completed implementer attempt artifact.
_IMPLEMENTER_ARTIFACT_SUFFIX = "-implementer.json"
#: Name tail of an attempt's start marker. The completed-artifact glob requires
#: the exact ``-implementer.json`` tail, so a marker never reads back as a
#: completed attempt.
_IMPLEMENTER_START_MARKER_SUFFIX = "-implementer.started.json"


def _implementer_evidence_root(progress_path: Path) -> Path:
    """Return the framework-owned directory of per-attempt implementer evidence."""
    return _structured_artifact_root(progress_path) / "evidence"


def write_implementer_artifact(
    progress_path: Path,
    round_number: int,
    retry: int,
    response: ImplementerResponse,
) -> Path:
    """Persist parsed implementer claims as untrusted data for Judge audit."""
    path = _implementer_evidence_root(progress_path) / (
        f"round-{round_number:04d}-attempt-{retry:02d}{_IMPLEMENTER_ARTIFACT_SUFFIX}"
    )
    return _write_json_atomic(path, response.model_dump(mode="json"))


def write_implementer_start_marker(progress_path: Path, round_number: int, retry: int) -> Path:
    """Record that one implementer attempt began, before its turn runs."""
    path = _implementer_evidence_root(progress_path) / (
        f"round-{round_number:04d}-attempt-{retry:02d}{_IMPLEMENTER_START_MARKER_SUFFIX}"
    )
    return _write_json_atomic(path, {"round": round_number, "attempt": retry})


def validation_artifact_root(progress_path: Path) -> Path:
    """Return the framework-owned validation ledger directory."""
    return _structured_artifact_root(progress_path) / "validation"


def profiler_artifact_root(progress_path: Path, round_number: int) -> Path:
    """Return the only durable output directory writable by a Profiler turn."""
    return _structured_artifact_root(progress_path) / "profiles" / f"round-{round_number:04d}"


def validation_recipe_schema_path(progress_path: Path) -> Path:
    """Return the framework-owned candidate recipe-schema path."""
    return validation_artifact_root(progress_path) / "recipe-schema.json"


def write_validation_recipe_schema(progress_path: Path) -> Path:
    """Publish the authoritative recipe contract for on-demand agent reads."""
    return _write_json_atomic(
        validation_recipe_schema_path(progress_path),
        ValidationRecipeArtifact.model_json_schema(mode="validation"),
    )


def write_validation_result_artifact(
    progress_path: Path,
    round_number: int,
    retry: int,
    results: list[FrameworkValidationResult],
) -> Path:
    """Persist framework-executed validation results for replay and reuse."""
    path = (
        validation_artifact_root(progress_path)
        / f"round-{round_number:04d}-attempt-{retry:02d}.json"
    )
    payload = {
        "round": round_number,
        "attempt": retry,
        "results": [result.model_dump(mode="json") for result in results],
    }
    return _write_json_atomic(path, payload)


def validation_result_artifact_paths(progress_path: Path) -> list[Path]:
    """Return validation result artifacts in deterministic creation order."""
    return sorted(validation_artifact_root(progress_path).glob("round-*-attempt-*.json"))


def implementer_artifact_paths(progress_path: Path, round_number: int) -> list[Path]:
    """Return persisted implementer attempts for one round in attempt order."""
    pattern = f"round-{round_number:04d}-attempt-*{_IMPLEMENTER_ARTIFACT_SUFFIX}"
    return sorted(_implementer_evidence_root(progress_path).glob(pattern))


def _implementer_attempt_numbers(progress_path: Path, round_number: int, suffix: str) -> list[int]:
    """Return the attempt numbers named by one round's *suffix* evidence files."""
    prefix = f"round-{round_number:04d}-attempt-"
    names = _implementer_evidence_root(progress_path).glob(f"{prefix}*{suffix}")
    attempts = (path.name.removeprefix(prefix).removesuffix(suffix) for path in names)
    return [int(attempt) for attempt in attempts if attempt.isdigit()]


def next_implementer_attempt(progress_path: Path, round_number: int) -> int:
    """Return the next durable attempt number for an interrupted round.

    Start markers count alongside completed artifacts, which makes the attempt
    number durable at attempt start rather than only once the turn returns. A
    process killed mid-invoke therefore resumes on a fresh attempt instead of
    replaying the killed attempt's round label.
    """
    attempts = [
        attempt
        for suffix in (_IMPLEMENTER_ARTIFACT_SUFFIX, _IMPLEMENTER_START_MARKER_SUFFIX)
        for attempt in _implementer_attempt_numbers(progress_path, round_number, suffix)
    ]
    return max(attempts, default=0) + 1


def pareto_archive_path(progress_path: Path) -> Path:
    """Return the framework-owned Pareto archive beside progress history."""
    if progress_path.suffix == ".md":
        return progress_path.with_name("pareto-frontier.md")
    return progress_path / "pareto-frontier.md"


def framework_memory_paths(workspace: Path) -> tuple[Path, ...]:
    """Return every memory location the framework writes into *workspace*.

    Both layouts are returned because a projection over historical commits
    cannot know which layout a past round used, and a resumed run may switch.
    The artifact and Pareto roots are derived from the progress path rather
    than restated, so a new framework-owned location under ``progress`` is
    covered by construction. The paths need not exist.
    """
    paths: list[Path] = []
    for name in MEMORY_LAYOUTS_ROOTS:
        paths.extend((workspace / f"{name}.md", workspace / name))
    for progress in (workspace / "progress.md", workspace / "progress"):
        paths.extend((_structured_artifact_root(progress), pareto_archive_path(progress)))
    return tuple(dict.fromkeys(paths))


def write_pareto_archive(progress_path: Path, summary: str) -> Path:
    """Materialize the derived frontier so agents can inspect it on demand."""
    document = pareto_archive_path(progress_path)
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text(f"# Pareto frontier\n\n{summary.rstrip()}\n")
    return document


def _roadmap_document(roadmap_path: Path) -> Path:
    return roadmap_path if roadmap_path.suffix == ".md" else roadmap_path / "index.md"


# ---------------------------------------------------------------------------
# roadmap.md — orchestrator's strategic memory
# ---------------------------------------------------------------------------


_ROADMAP_HEADER = """# Roadmap

You (the Orchestrator) own this file end-to-end. Update it every round
*before* deciding the round's task. The framework names this file in
your next prompt but does not inject or parse its contents, so inspect it
with tools and format it however you find useful. Follow these conventions
so the structure stays legible:

- **Major** items: structural changes expected to move the headline
  performance metric meaningfully. Derive them from measured bottlenecks and
  the objective rather than from examples supplied by the framework. Usually
  1-3 rounds each.
- **Minor** items: bug fixes, polish, gates (correctness recoveries,
  tiny kernel swaps, accuracy bumps). Usually 1 round each.
- Use one of these four statuses, and note rounds spent on each
  in-progress item:
  - `todo` — not started.
  - `in_progress` — actively being worked on this round (or recent rounds).
  - `done` — implemented, profiler-verified, hitting (close to) predicted impact.
  - `parked` — implementation is buggy or incomplete, but you believe the
    *direction* is sound. Returnable to `in_progress` later. Use this when
    the metric isn't moving for an *implementation* reason rather than a
    workload reason.
  - `abandoned` — the *direction* itself doesn't fit this workload. Strict
    requirement (see below) before flipping to this state.
- For each item include a one-line *why* (predicted impact, what
  bottleneck it addresses).

If any Major item is `todo` or `in_progress`, this round's task should
serve it. Do NOT drop into Minor work while a Major sits unfinished
unless that Minor is genuinely blocking the Major (state the dependency
explicitly when you do).

## `parked` vs `abandoned` — get this distinction right

These two are not the same thing and the loop's behavior degrades if you
treat them as one bucket:

- **`parked`** is the right call when (a) you predicted the change would help,
  (b) the implementation satisfies correctness gates, but (c) the headline
  metric did not move because the intended path did not activate or the
  implementation is incomplete. The direction itself remains believable.
  Mark it `parked`, move to a different Major, and return when you have a
  concrete debugging hypothesis or other measured avenues are exhausted.

- **`abandoned`** is the right call only when the *direction itself* is the
  wrong fit for this workload. It requires a mechanism-level autopsy explaining
  why the change cannot help here, not merely that a few measurements were
  flat. If you cannot write that mechanism, use `parked` instead.

**Hard rule for `abandoned` autopsies:** name a code-level, system-level, or
hardware-level mechanism—not a behavioral observation. A flat performance
number alone is not a mechanism. If activation evidence is absent, treat that
as a debugging task and use `parked` with a concrete hypothesis.

## Major

(populate on round 1 based on the objective)

## Minor

(none yet)

## Done

(none yet)

## Parked

(none yet)

## Abandoned

(none yet)
"""


def ensure_roadmap_file(roadmap_path: Path) -> None:
    """Create the roadmap with the seed header if it doesn't exist.

    Idempotent; safe to call every round.
    """
    document = _roadmap_document(roadmap_path)
    if not document.exists():
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_text(_ROADMAP_HEADER)


def read_roadmap(roadmap_path: Path) -> str:
    """Return the current roadmap contents, or an empty string if missing."""
    document = _roadmap_document(roadmap_path)
    if not document.exists():
        return ""
    return document.read_text()


# ---------------------------------------------------------------------------
# progress.md — per-round audit log
# ---------------------------------------------------------------------------


_PROGRESS_HEADER = "# Progress\n\n"
_PROGRESS_README = """# Progress

Each round has its own `round-NNNN.md` audit log. Agent prompts name this
directory; agents inspect only the rounds relevant to the current decision.
"""


def ensure_progress_file(progress_path: Path) -> None:
    """Create the progress file with a header if it doesn't exist."""
    if progress_path.suffix == ".md" and not progress_path.exists():
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        progress_path.write_text(_PROGRESS_HEADER)
    elif progress_path.suffix != ".md":
        progress_path.mkdir(parents=True, exist_ok=True)
        readme = progress_path / "README.md"
        if not readme.exists():
            readme.write_text(_PROGRESS_README)


def read_progress(progress_path: Path, *, recent_rounds: int = _RECENT_PROGRESS_ROUNDS) -> str:
    """Return progress, bounded to recent per-round files in directory mode."""
    if not progress_path.exists():
        return ""
    if progress_path.is_file():
        return progress_path.read_text()
    round_files = sorted(progress_path.glob("round-[0-9][0-9][0-9][0-9].md"))
    selected = round_files[-recent_rounds:] if recent_rounds > 0 else round_files
    return "\n\n".join(path.read_text().rstrip() for path in selected)


def _append(progress_path: Path, block: str, round_number: int) -> None:
    """Write one framework-owned progress section idempotently.

    A run can be resumed after a process exits between recording a phase result
    and finishing the round.  The resumed phase has the same stable Markdown
    heading (round, role, and attempt), so replace that section instead of
    appending a duplicate.  Distinct attempts retain distinct headings and
    therefore remain separate audit entries.
    """
    ensure_progress_file(progress_path)
    document = (
        progress_path
        if progress_path.suffix == ".md"
        else progress_path / f"round-{round_number:04d}.md"
    )
    if not document.exists() and document != progress_path:
        document.write_text(f"# Round {round_number}\n\n")

    heading = block.splitlines()[0]
    normalized_block = block.rstrip("\n") + "\n\n"
    lines = document.read_text(encoding="utf-8").splitlines(keepends=True)
    output: list[str] = []
    replaced = False
    index = 0
    while index < len(lines):
        if lines[index].rstrip("\r\n") != heading:
            output.append(lines[index])
            index += 1
            continue

        if not replaced:
            output.append(normalized_block)
            replaced = True
        index += 1
        # A framework section owns its H3 children, but not a neighboring H2.
        # Operators and recovery tooling may append evidence under their own H2
        # between an interrupted phase and resume. Preserve that evidence when
        # replacing the stable framework heading.
        while index < len(lines) and not lines[index].startswith("## "):
            index += 1

    if not replaced:
        with document.open("a", encoding="utf-8") as fh:
            fh.write(normalized_block)
        return

    # Replacement rewrites an existing audit section.  Keep the prior file
    # intact if the process exits during the write, then atomically publish the
    # completed document.
    replacement = document.with_name(f".{document.name}.tmp")
    replacement.write_text("".join(output), encoding="utf-8")
    replacement.replace(document)


def append_pre_round_decision(
    progress_path: Path, round_number: int, decision: PreRoundDecision
) -> None:
    """Append the orchestrator's pre-round decision to progress history."""
    block = (
        f"## Round {round_number} — Orchestrator (pre-round)\n"
        f"- **need_profile**: {decision.need_profile}\n"
        f"- **profile_focus**: {decision.profile_focus}\n"
        f"- **reasoning**: {decision.reasoning}\n"
    )
    _append(progress_path, block, round_number)


def append_profiler_summary(
    progress_path: Path, round_number: int, summary: ProfilerSummary
) -> None:
    """Append a profiler summary for one round."""
    perf_line = ""
    if summary.perf_metric is not None:
        unit = summary.perf_unit or ""
        perf_line = f"- **perf_metric**: {summary.perf_metric} {unit}\n".rstrip() + "\n"
    block = (
        f"## Round {round_number} — Profiler\n"
        f"{perf_line}"
        f"### Bottlenecks\n{summary.bottlenecks}\n\n"
        f"### Suggestions\n{summary.suggestions}\n\n"
        f"### Analysis\n{summary.analysis}\n"
    )
    _append(progress_path, block, round_number)


def append_orchestrator_plan(
    progress_path: Path, round_number: int, plan: OrchestratorPlan
) -> None:
    """Append the orchestrator plan and any requested round rollback."""
    revert_line = ""
    if plan.revert_to_round is not None:
        revert_line = f"- **revert_to_round**: {plan.revert_to_round}\n"
    strategy_lines = "".join(
        f"- **hypothesis_update**: {update.hypothesis_id} -> "
        f"{update.disposition}: {update.reason}\n"
        for update in plan.hypothesis_updates
    )
    block = (
        f"## Round {round_number} — Orchestrator (plan)\n"
        f"{revert_line}"
        f"{strategy_lines}"
        f"- **hypothesis_id**: {plan.hypothesis_id or '(unspecified)'}\n"
        f"- **reasoning**: {plan.reasoning}\n\n"
        f"### Hypothesis\n{plan.hypothesis or '(unspecified)'}\n\n"
        f"### Activation evidence\n{plan.activation_evidence or '(unspecified)'}\n\n"
        f"### Falsification criteria\n{plan.falsification_criteria or '(unspecified)'}\n\n"
        f"### Expected effect (forecast)\n{plan.expected_effect or '(unspecified)'}\n\n"
        "### Minimum acceptance criteria\n"
        f"{plan.minimum_acceptance_criteria or '(unspecified)'}\n\n"
        f"### Invariants\n{plan.invariants or '(unspecified)'}\n\n"
        f"### Task\n{plan.task}\n\n"
        f"### Pass criteria\n{plan.pass_criteria}\n"
    )
    _append(progress_path, block, round_number)


def append_hypothesis_continuation(
    progress_path: Path,
    round_number: int,
    *,
    plan: OrchestratorPlan,
    started_round: int,
    continuation_step: str,
) -> None:
    """Append the continuation plan used to refine the active hypothesis."""
    block = (
        f"## Round {round_number} — Active hypothesis continuation\n"
        f"- **hypothesis_id**: {plan.hypothesis_id}\n"
        f"- **started_round**: {started_round}\n"
        "- **designer_invocation**: skipped; implementer retains ownership\n\n"
        f"### Hypothesis\n{plan.hypothesis or '(unspecified)'}\n\n"
        f"### Current continuation delta\n{continuation_step}\n"
    )
    _append(progress_path, block, round_number)


def append_implementer(
    progress_path: Path, round_number: int, retry: int, response: ImplementerResponse
) -> None:
    """Append one implementer attempt and its reported result."""
    perf_line = ""
    if response.perf_metric is not None:
        unit = response.perf_unit or ""
        perf_line = (
            f"- **perf_metric**: {response.perf_metric} {unit}\n".rstrip()
            + "\n"
            + f"- **metrics**: {response.metrics}\n"
            + f"- **evaluation_artifact**: {response.evaluation_artifact or '(missing)'}\n"
        )
    candidate_line = (
        f"- **candidate_disposition**: {response.candidate_disposition.value}\n"
        f"- **candidate_metrics**: {response.candidate_metrics or {}}\n"
        "- **candidate_evaluation_artifact**: "
        f"{response.candidate_evaluation_artifact or '(missing)'}\n"
        f"- **candidate_operating_point**: {response.candidate_operating_point or '(none)'}\n"
        "- **candidate_retention_reason**: "
        f"{response.candidate_retention_reason or '(none)'}\n"
    )
    block = (
        f"## Round {round_number} — Implementer (attempt {retry})\n"
        f"- **expected_behavior**: {response.expected_behavior}\n"
        f"- **hypothesis_outcome**: {response.hypothesis_outcome.value}\n"
        f"- **next_step**: {response.next_step or '(none)'}\n\n"
        f"{perf_line}"
        f"{candidate_line}\n"
        f"### Summary\n{response.summary}\n\n"
        f"### Evidence\n{response.evidence or '(none)'}\n"
    )
    _append(progress_path, block, round_number)


def append_judge(
    progress_path: Path, round_number: int, retry: int, response: JudgeResponse
) -> None:
    """Append one judge response to the round history."""
    block = (
        f"## Round {round_number} — Judge (attempt {retry})\n"
        f"- **verdict**: {response.verdict.value}\n\n"
        f"### Analysis\n{response.analysis}\n\n"
        f"### Feedback\n{response.feedback}\n"
    )
    _append(progress_path, block, round_number)


def append_judge_skipped(
    progress_path: Path,
    round_number: int,
    *,
    outcome: str,
    judge_every: int,
) -> None:
    """Append the reason the judge step was skipped."""
    block = (
        f"## Round {round_number} — Independent review deferred\n"
        f"- **implementer_outcome**: {outcome}\n"
        f"- **policy**: review every {judge_every} rounds, on nomination, and on the final round\n"
        "- **official_gates**: not run; all evidence this round is provisional\n"
    )
    _append(progress_path, block, round_number)


def append_official_evaluation_decision(
    progress_path: Path,
    round_number: int,
    retry: int,
    *,
    run: bool,
    reason: str,
    official_eval_every: int,
    provisional_candidates: int,
) -> None:
    """Append the framework's official evaluation decision."""
    decision = "run" if run else "deferred"
    block = (
        f"## Round {round_number} — Official evaluation policy (attempt {retry})\n"
        f"- **decision**: {decision}\n"
        f"- **reason**: {reason}\n"
        f"- **cadence**: every {official_eval_every} accepted candidate checkpoints\n"
        f"- **provisional_candidates_before_this_round**: {provisional_candidates}\n"
    )
    _append(progress_path, block, round_number)


def append_single_agent_round(
    progress_path: Path,
    round_number: int,
    retry: int,
    response: SingleAgentRoundResponse,
) -> None:
    """Append the result of a single-agent round."""
    perf_line = ""
    if response.perf_metric is not None:
        unit = response.perf_unit or ""
        perf_line = f"- **perf_metric**: {response.perf_metric} {unit}\n".rstrip() + "\n"
    candidate_line = (
        f"- **candidate_disposition**: {response.candidate_disposition.value}\n"
        f"- **candidate_metrics**: {response.candidate_metrics or {}}\n"
        "- **candidate_evaluation_artifact**: "
        f"{response.candidate_evaluation_artifact or '(missing)'}\n"
        f"- **candidate_operating_point**: {response.candidate_operating_point or '(none)'}\n"
        "- **candidate_retention_reason**: "
        f"{response.candidate_retention_reason or '(none)'}\n"
    )
    block = (
        f"## Round {round_number} — Single-agent (attempt {retry})\n"
        f"- **verdict**: {response.verdict.value}\n"
        f"- **expected_behavior**: {response.expected_behavior}\n"
        f"{perf_line}"
        f"{candidate_line}"
        f"### Summary\n{response.summary}\n\n"
        f"### Self-review\n{response.self_review}\n\n"
        f"### Feedback\n{response.feedback}\n\n"
        f"### Bottlenecks\n{response.bottlenecks}\n\n"
        f"### Suggestions\n{response.suggestions}\n\n"
        f"### Profile analysis\n{response.profile_analysis}\n"
    )
    _append(progress_path, block, round_number)


def append_framework_accuracy_gate(
    progress_path: Path,
    round_number: int,
    retry: int,
    *,
    command: str,
    passed: bool,
    output: str,
) -> None:
    """Append the framework accuracy-gate result."""
    verdict = "pass" if passed else "fail"
    block = (
        f"## Round {round_number} — Framework accuracy gate (attempt {retry})\n"
        f"- **verdict**: {verdict}\n"
        f"- **command**: `{command}`\n\n"
        f"### Output\n{output or '(no output)'}\n"
    )
    _append(progress_path, block, round_number)


def append_framework_validation_gate(
    progress_path: Path,
    round_number: int,
    retry: int,
    *,
    artifact: str,
    results: list[FrameworkValidationResult],
) -> None:
    """Record the deterministic local validation gate in the progress ledger."""
    passed = bool(results) and all(result.passed for result in results)
    lines = [
        f"## Round {round_number} — Framework local validation (attempt {retry})",
        f"- **verdict**: {'pass' if passed else 'fail'}",
        f"- **artifact**: `{artifact}`",
    ]
    for result in results:
        source = "reused" if result.reused else "executed"
        lines.append(
            f"- **{result.recipe.name}**: {'pass' if result.passed else 'fail'} "
            f"({source}, inputs `{result.input_digest[:12]}`)"
        )
    _append(progress_path, "\n".join(lines) + "\n", round_number)


def append_framework_benchmark(
    progress_path: Path,
    round_number: int,
    retry: int,
    *,
    command: str,
    passed: bool,
    metric_name: str | None,
    metric_value: float | None,
    output: str,
) -> None:
    """Append framework benchmark metrics and diagnostics."""
    verdict = "pass" if passed else "fail"
    metric_line = (
        f"- **{metric_name}**: {metric_value}\n"
        if metric_name is not None and metric_value is not None
        else ""
    )
    block = (
        f"## Round {round_number} — Framework benchmark (attempt {retry})\n"
        f"- **verdict**: {verdict}\n"
        f"- **command**: `{command}`\n"
        f"{metric_line}\n"
        f"### Output\n{output or '(no output)'}\n"
    )
    _append(progress_path, block, round_number)


def append_exhaustion_note(
    progress_path: Path, round_number: int, attempts: int, last_feedback: str
) -> None:
    """Append a note that judge retries were exhausted."""
    block = (
        f"## Round {round_number} — Judge loop exhausted\n"
        f"- **attempts**: {attempts}\n"
        f"- **last_feedback**: {last_feedback}\n"
    )
    _append(progress_path, block, round_number)
