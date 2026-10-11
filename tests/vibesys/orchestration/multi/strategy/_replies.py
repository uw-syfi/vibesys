"""Scripted agent replies in the shapes the multi roles are asked to produce."""

from __future__ import annotations

import json


def pre_round_reply(*, need_profile: bool = False, focus: str = "") -> str:
    """The routing decision: whether fresh profile evidence is wanted."""
    return json.dumps(
        {"need_profile": need_profile, "profile_focus": focus, "reasoning": "because"}
    )


def profiler_reply(**extra: object) -> str:
    """The profiler's findings."""
    return json.dumps(
        {"analysis": "analysis", "bottlenecks": "hot loop", "suggestions": "fuse it", **extra}
    )


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


def implemented(outcome: str = "nominated", **extra: object) -> str:
    """An implementer reply."""
    return json.dumps(
        {
            "summary": "did it",
            "expected_behavior": "faster",
            "hypothesis_outcome": outcome,
            **extra,
        }
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
