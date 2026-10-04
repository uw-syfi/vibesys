"""Scripted agent replies in the shapes the dynamic roles are asked to produce."""

from __future__ import annotations

import json


def plan_reply(*entries: dict[str, object], reasoning: str = "portfolio") -> str:
    """A planner reply of implement or profile entries."""
    return json.dumps({"reasoning": reasoning, "workstreams": list(entries)})


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


def implemented(outcome: str = "supported", summary: str = "done", **extra: object) -> str:
    """An implementer result reply."""
    return json.dumps({"summary": summary, "outcome": outcome, **extra})


def reviewed(*, passed: bool = True) -> str:
    """A judge result reply."""
    return json.dumps(
        {"passed": passed, "analysis": "analysis", "feedback": "" if passed else "fix"}
    )
