"""Scripted agent replies in the shapes the dynamic roles are asked to produce."""

from __future__ import annotations

import json


def plan_reply(
    *entries: dict[str, object],
    reasoning: str = "portfolio",
    handoff: dict[str, object] | None = None,
) -> str:
    """A planner reply of implement or profile entries, with a handoff note for each entry."""
    note = (
        handoff
        if handoff is not None
        else {
            "expectations": [expectation(_entry_id(entry)) for entry in entries],
            "doubts": ["doubt"],
        }
    )
    return json.dumps({"reasoning": reasoning, "workstreams": list(entries), "handoff": note})


def _entry_id(entry: dict[str, object]) -> str:
    return str(entry.get("hypothesis_id", entry.get("profile_id", "unknown")))


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
