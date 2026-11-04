"""Scripted agent replies in the shapes the dynamic roles are asked to produce."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


def plan_reply(
    *entries: dict[str, object],
    reasoning: str = "portfolio",
    handoff: dict[str, object] | None = None,
    updates: Sequence[dict[str, object]] = (),
) -> str:
    """A planner reply of implement or profile entries, with a handoff note for each entry.

    ``updates`` are the reply's hypothesis updates (`abandon`), left out of the reply when empty.
    """
    note = (
        handoff
        if handoff is not None
        else {
            "expectations": [expectation(_entry_id(entry)) for entry in entries],
            "doubts": ["doubt"],
        }
    )
    reply: dict[str, object] = {"reasoning": reasoning, "workstreams": list(entries)}
    if updates:
        reply["hypothesis_updates"] = list(updates)
    return json.dumps({**reply, "handoff": note})


def abandon(identifier: str) -> dict[str, object]:
    """One hypothesis update of a plan that abandons ``identifier``."""
    return {
        "hypothesis_id": identifier,
        "disposition": "abandoned",
        "reason": f"retire {identifier}",
        "reason_kind": "falsified",
    }


def _entry_id(entry: dict[str, object]) -> str:
    return str(entry.get("hypothesis_id", entry.get("profile_id", entry.get("merge_id", "?"))))


def expectation(identifier: str, **extra: object) -> dict[str, object]:
    """One handoff expectation of a planned workstream."""
    return {
        "work_id": identifier,
        "expected": f"expected {identifier}",
        "if_it_succeeds": f"build on {identifier}",
        "if_it_fails": f"drop {identifier}",
        **extra,
    }


def implement(identifier: str, **extra: object) -> dict[str, object]:
    """One implement entry of a plan."""
    return {
        "kind": "implement",
        "hypothesis_id": identifier,
        "title": f"title {identifier}",
        "hypothesis": f"claim {identifier}",
        "task": f"task {identifier}",
        "pass_criteria": "faster",
        **extra,
    }


def profile(identifier: str, **extra: object) -> dict[str, object]:
    """One trusted profile entry of a plan."""
    return {
        "kind": "profile",
        "profile_id": identifier,
        "target_hypothesis_id": None,
        "question": "Where does the current revision spend its time?",
        "decision_impact": "Optimize the dominant component next.",
        **extra,
    }


def merge(identifier: str, *candidates: str, **extra: object) -> dict[str, object]:
    """One merge entry of a plan: the hypotheses whose latest candidates to merge into main."""
    return {
        "kind": "merge",
        "merge_id": identifier,
        "candidates": list(candidates),
        "instructions": f"apply {', '.join(candidates)} in order",
        "watch_for": "both touch the scheduler",
        **extra,
    }


def combine(identifier: str, *parents: tuple[str, str], **extra: object) -> dict[str, object]:
    """One combine entry of a plan: a new hypothesis built from exact (hypothesis, revision) parts."""
    return {
        "kind": "combine",
        "hypothesis_id": identifier,
        "title": f"title {identifier}",
        "hypothesis": f"the parts of {identifier} work together",
        "parents": [{"hypothesis_id": item, "revision": revision} for item, revision in parents],
        "instructions": "apply them in order; on a conflict keep both behaviors",
        "watch_for": "both touch the scheduler",
        "pass_criteria": "faster than the best part",
        **extra,
    }


def merged(outcome: str = "merged", reason: str = "") -> str:
    """A merger result reply."""
    return json.dumps(
        {"outcome": outcome, "summary": "merged in order", "tests": "pytest: ok", "reason": reason}
    )


def implemented(outcome: str = "supported", summary: str = "done", **extra: object) -> str:
    """An implementer result reply."""
    return json.dumps({"summary": summary, "outcome": outcome, **extra})


def reviewed(*, passed: bool = True) -> str:
    """A judge result reply."""
    return json.dumps(
        {"passed": passed, "analysis": "analysis", "feedback": "" if passed else "fix"}
    )


def step(
    identifier: str, evaluate: str = "none", depends_on: tuple[str, ...] = ()
) -> dict[str, object]:
    """One step of a task's plan, as its ``[plan]`` table or an outline reply writes it."""
    return {
        "id": identifier,
        "title": f"title {identifier}",
        "documents": [],
        "done_when": f"the tests of {identifier} pass",
        "evaluate": evaluate,
        "depends_on": list(depends_on),
    }


def outline_reply(*steps: dict[str, object]) -> str:
    """An outliner reply of plan steps."""
    return json.dumps({"steps": list(steps)})
