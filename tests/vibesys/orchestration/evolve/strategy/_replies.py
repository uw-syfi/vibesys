"""Scripted agent replies in the shapes the evolve roles are asked to produce."""

from __future__ import annotations

import json


def mutated(summary: str = "did it", **extra: object) -> str:
    """A mutator reply."""
    return json.dumps(
        {"summary": summary, "hypothesis": "because", "expected_behavior": "faster", **extra}
    )


def reviewed(*, passed: bool = True, **extra: object) -> str:
    """A judge reply."""
    return json.dumps(
        {
            "analysis": "checked",
            "feedback": "" if passed else "fix it",
            "verdict": "pass" if passed else "fail",
            **extra,
        }
    )


def profiled(**extra: object) -> str:
    """The profiler's findings."""
    return json.dumps(
        {"analysis": "analysis", "bottlenecks": "hot loop", "suggestions": "fuse it", **extra}
    )
