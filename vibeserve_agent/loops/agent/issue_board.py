"""The agent loop's issue board (roadmap.md + progress.md).

The agent loop's "issue board" is two markdown files in the workspace:

  - ``roadmap.md`` — strategic memory the Orchestrator owns end-to-end.
    Free-form; the framework only seeds the header on round 1 and threads
    the contents back into the orchestrator's prompt each round.
  - ``progress.md`` — per-round audit log. The orchestrator, profiler,
    implementer, and judge each append one block per round; the
    orchestrator reads the full file when planning the next round.

Both surfaces together are this loop's planning artifact, parallel to
the plain loop's structured :class:`~vibeserve_agent.loops.plain.issue_board.IssueBoard`
(``issues.json``).
"""

from __future__ import annotations

from pathlib import Path

from vibeserve_agent.schemas import (
    OrchestratorPlan,
    PreRoundDecision,
    ProfilerSummary,
)
from vibeserve_agent.schemas import (
    ImplementerResponse,
    JudgeResponse,
)


# ---------------------------------------------------------------------------
# roadmap.md — orchestrator's strategic memory
# ---------------------------------------------------------------------------


_ROADMAP_HEADER = """# Roadmap

You (the Orchestrator) own this file end-to-end. Update it every round
*before* deciding the round's task. The framework reads it back into
your next prompt — it does not parse it, so format it however you find
useful, but follow these conventions so the structure stays legible:

- **Major** items: structural changes expected to move the headline
  performance metric meaningfully (e.g. "Implement EAGLE3 speculative
  decoding", "Add CUDA graphs to verifier decode", "Replace manual
  attention with FlashAttention"). Usually 1-3 rounds each.
- **Minor** items: bug fixes, polish, gates (correctness recoveries,
  tiny kernel swaps, accuracy bumps). Usually 1 round each.
- Use one of these four statuses, and note rounds spent on each
  in-progress item:
  - `todo` — not started.
  - `in_progress` — actively being worked on this round (or recent rounds).
  - `done` — implemented, profiler-verified, hitting (close to) predicted impact.
  - `parked` — implementation is buggy or incomplete, but you believe the
    *direction* is sound. Returnable to `in_progress` later. Use this when
    the metric isn't moving for an *implementation* reason (zero acceptance
    on EAGLE3, capture failures on CUDA graphs, …) rather than a workload
    reason.
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

- **`parked`** is the right call when (a) you predicted the technique
  would help, (b) the implementation passes the judge / pytest / accuracy
  gate, but (c) the headline metric didn't move *because the implementation
  appears to have a bug or is incomplete*. Symptoms: zero acceptance on a
  speculative decoder, all-fallback paths on what should be the fast path,
  a CUDA graph capturing but never replaying, etc. The direction itself is
  still believable; you just couldn't make the implementation good enough
  in the rounds you spent. Mark `parked` and move to a different Major;
  return to it when (i) you have a hypothesis for the bug, or (ii) other
  levers are exhausted.

- **`abandoned`** is the right call only when the *direction itself* is
  the wrong fit for this workload. Examples: continuous batching on a
  workload contractually limited to single-batch, MTP on a model that
  doesn't ship MTP heads, paged attention when the engine's fixed-shape
  KV path is already optimal at this batch size. Each requires a
  *mechanism-level* autopsy explaining why the technique cannot help
  *here* (not "it didn't pay off in 3 rounds"). If you can't write that
  mechanism, the right status is `parked`, not `abandoned`.

**Hard rule for `abandoned` autopsies:** you must name a code-level or
hardware-level mechanism — not a behavioral observation. A perf number
("+0% TPOT") is not a mechanism; "the workload is single-batch by
contract so continuous batching cannot raise arithmetic intensity" is.
If acceptance on a speculative decoder is zero, that is *not* a reason
to abandon — it's a debugging task, and the right status is `parked`
with a hypothesis. Spec decode acceptance debugging has a checklist in
`references/algorithms/speculative-decoding.md` ("Debugging 0
acceptance"); read it before parking or abandoning.

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
    if not roadmap_path.exists():
        roadmap_path.parent.mkdir(parents=True, exist_ok=True)
        roadmap_path.write_text(_ROADMAP_HEADER)


def read_roadmap(roadmap_path: Path) -> str:
    """Return the current roadmap contents, or an empty string if missing.

    Callers thread this into the orchestrator's prompt verbatim.
    """
    if not roadmap_path.exists():
        return ""
    return roadmap_path.read_text()


# ---------------------------------------------------------------------------
# progress.md — per-round audit log
# ---------------------------------------------------------------------------


_PROGRESS_HEADER = "# Progress\n\n"


def ensure_progress_file(progress_path: Path) -> None:
    """Create the progress file with a header if it doesn't exist."""
    if not progress_path.exists():
        progress_path.write_text(_PROGRESS_HEADER)


def read_progress(progress_path: Path) -> str:
    """Return the full progress file contents or an empty string if missing."""
    if not progress_path.exists():
        return ""
    return progress_path.read_text()


def _append(progress_path: Path, block: str) -> None:
    ensure_progress_file(progress_path)
    with progress_path.open("a", encoding="utf-8") as fh:
        if not block.endswith("\n"):
            block += "\n"
        fh.write(block + "\n")


def append_pre_round_decision(
    progress_path: Path, round_number: int, decision: PreRoundDecision
) -> None:
    block = (
        f"## Round {round_number} — Orchestrator (pre-round)\n"
        f"- **need_profile**: {decision.need_profile}\n"
        f"- **profile_focus**: {decision.profile_focus}\n"
        f"- **reasoning**: {decision.reasoning}\n"
    )
    _append(progress_path, block)


def append_profiler_summary(
    progress_path: Path, round_number: int, summary: ProfilerSummary
) -> None:
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
    _append(progress_path, block)


def append_orchestrator_plan(
    progress_path: Path, round_number: int, plan: OrchestratorPlan
) -> None:
    revert_line = ""
    if plan.revert_to_round is not None:
        revert_line = f"- **revert_to_round**: {plan.revert_to_round}\n"
    block = (
        f"## Round {round_number} — Orchestrator (plan)\n"
        f"{revert_line}"
        f"- **reasoning**: {plan.reasoning}\n\n"
        f"### Task\n{plan.task}\n\n"
        f"### Pass criteria\n{plan.pass_criteria}\n"
    )
    _append(progress_path, block)


def append_implementer(
    progress_path: Path, round_number: int, retry: int, response: ImplementerResponse
) -> None:
    block = (
        f"## Round {round_number} — Implementer (attempt {retry})\n"
        f"- **expected_behavior**: {response.expected_behavior}\n\n"
        f"### Summary\n{response.summary}\n"
    )
    _append(progress_path, block)


def append_judge(
    progress_path: Path, round_number: int, retry: int, response: JudgeResponse
) -> None:
    block = (
        f"## Round {round_number} — Judge (attempt {retry})\n"
        f"- **verdict**: {response.verdict.value}\n\n"
        f"### Analysis\n{response.analysis}\n\n"
        f"### Feedback\n{response.feedback}\n"
    )
    _append(progress_path, block)


def append_exhaustion_note(
    progress_path: Path, round_number: int, attempts: int, last_feedback: str
) -> None:
    block = (
        f"## Round {round_number} — Judge loop exhausted\n"
        f"- **attempts**: {attempts}\n"
        f"- **last_feedback**: {last_feedback}\n"
    )
    _append(progress_path, block)
