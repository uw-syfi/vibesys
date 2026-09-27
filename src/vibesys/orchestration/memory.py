"""Host-owned agent memory: roadmap, progress log, and Pareto archive.

Every agent strategy needs a durable place to write its own planning notes
(the *roadmap*) and per-round audit trail (*progress*), plus a
framework-materialized Pareto archive derived from committed state. These
The roadmap lives at ``roadmap/index.md``. Progress uses one file per round at
``progress/round-NNNN.md``, which stays scannable when a run grows to hundreds
of rounds.

A strategy declares these paths once, through ``OrchestrationPlugin.memory_paths``
(:func:`declared_memory_paths`); the host then preserves them across
``workspaces.adopt``/``restore``/``transaction`` (see
``vs_runtime.api.OrchestrationPlugin.memory_paths``). This module owns creating and
writing to them: strategies call these functions directly rather than
hand-rolling file I/O.
"""

from __future__ import annotations

from pathlib import Path

MEMORY_ROOTS = ("roadmap", "progress")


def resolve_paths(workspace: Path) -> tuple[Path, Path]:
    """Resolve the canonical roadmap and progress directories."""
    roadmap, progress = MEMORY_ROOTS
    return workspace / roadmap, workspace / progress


def structured_artifact_root(progress_path: Path) -> Path:
    """Return the framework-owned directory for typed role handoffs.

    Shared with :mod:`vibesys.orchestration.artifacts`, which writes into this
    same root.
    """
    return progress_path


def pareto_archive_path(progress_path: Path) -> Path:
    """Return the framework-owned Pareto archive beside progress history."""
    return progress_path / "pareto-frontier.md"


def framework_memory_paths(workspace: Path) -> tuple[Path, ...]:
    """Return every memory location the framework writes into *workspace*.

    The paths need not exist. Derived framework artifacts live below the
    declared progress root, so declaring that root covers them by construction.
    """
    return tuple(workspace / name for name in MEMORY_ROOTS)


def declared_memory_paths() -> tuple[str, ...]:
    """Return :func:`framework_memory_paths` as workspace-relative strings.

    Every memory location is already workspace-relative in shape (it is
    built by joining fixed names under the workspace root), so resolving it
    against ``Path(".")`` and stringifying gives the same paths a strategy
    would compute per-run with ``path.relative_to(workspace_root)``. This is
    the form ``OrchestrationPlugin.memory_paths`` declares once, before the run's
    workspace path is known.
    """
    return tuple(str(path) for path in framework_memory_paths(Path()))


def write_pareto_archive(progress_path: Path, summary: str) -> Path:
    """Materialize the derived frontier so agents can inspect it on demand."""
    document = pareto_archive_path(progress_path)
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text(f"# Pareto frontier\n\n{summary.rstrip()}\n")
    return document


def _roadmap_document(roadmap_path: Path) -> Path:
    return roadmap_path / "index.md"


# ---------------------------------------------------------------------------
# Roadmap: orchestrator's strategic memory
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
# Progress: per-round audit log
# ---------------------------------------------------------------------------


_PROGRESS_README = """# Progress

Each round has its own `round-NNNN.md` audit log. Agent prompts name this
directory; agents inspect only the rounds relevant to the current decision.
"""

#: Kept as a bounded read helper for callers that explicitly request recent
#: audit text. Agent prompts receive only the durable path and inspect it
#: with tools.
_RECENT_PROGRESS_ROUNDS = 4


def ensure_progress_file(progress_path: Path) -> None:
    """Create the progress directory and its explanatory README."""
    progress_path.mkdir(parents=True, exist_ok=True)
    readme = progress_path / "README.md"
    if not readme.exists():
        readme.write_text(_PROGRESS_README)


def read_progress(progress_path: Path, *, recent_rounds: int = _RECENT_PROGRESS_ROUNDS) -> str:
    """Return progress, bounded to recent per-round files."""
    if not progress_path.exists():
        return ""
    round_files = sorted(progress_path.glob("round-[0-9][0-9][0-9][0-9].md"))
    selected = round_files[-recent_rounds:] if recent_rounds > 0 else round_files
    return "\n\n".join(path.read_text().rstrip() for path in selected)
