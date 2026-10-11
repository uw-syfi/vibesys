"""Scripted agent replies in the shapes the single roles are asked to produce."""

from __future__ import annotations

import json


def plan_reply(identifier: str = "h1", **extra: object) -> str:
    """A designer reply: one new hypothesis."""
    return json.dumps(
        {
            "hypothesis_id": identifier,
            "title": f"title {identifier}",
            "hypothesis": f"claim {identifier}",
            "task": f"task {identifier}",
            "pass_criteria": "faster",
            "reasoning": "because",
            **extra,
        }
    )


def combined(*, passed: bool = True, **extra: object) -> str:
    """A combined implement, self-review and profile reply."""
    return json.dumps(
        {
            "summary": "did it",
            "expected_behavior": "faster",
            "self_review": "ok",
            "feedback": "" if passed else "fix it",
            "verdict": "pass" if passed else "fail",
            "bottlenecks": "",
            "suggestions": "",
            "profile_analysis": "",
            **extra,
        }
    )
