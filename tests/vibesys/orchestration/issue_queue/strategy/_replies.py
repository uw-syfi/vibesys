"""Scripted agent replies in the shapes the issue-queue roles are asked to produce."""

from __future__ import annotations

import json


def implemented(summary: str = "done", **extra: object) -> str:
    """An implementer result reply."""
    return json.dumps({"summary": summary, "files_touched": ["a.py"], "self_check": "ok", **extra})


def judged(*, passed: bool = True, bugs: tuple[str, ...] = (), **extra: object) -> str:
    """A judge result reply, optionally filing bugs."""
    return json.dumps(
        {
            "analysis": "analysis",
            "feedback": "" if passed else "fix it",
            "verdict": "pass" if passed else "fail",
            "new_issues": [
                {"type": "bug", "title": title, "description": f"## Background\n{title}"}
                for title in bugs
            ],
            **extra,
        }
    )


def evaluated(*titles: str, kinds: tuple[str, ...] = (), **extra: object) -> str:
    """A performance evaluator reply filing one issue per title."""
    return json.dumps(
        {
            "analysis": "analysis",
            "metrics": {
                "load_levels": [
                    {
                        "target_rate": 1.0,
                        "actual_rate": 1.0,
                        "num_requests": 4,
                        "num_completed": 4,
                        "num_failed": 0,
                        "duration": 1.0,
                        "throughput": {"request_throughput": 4.0, "token_throughput": 40.0},
                    }
                ],
                "extra": {},
            },
            "evaluator_feedback": ["measure more"],
            "new_issues": [
                {
                    "type": kinds[index] if index < len(kinds) else "perf",
                    "title": title,
                    "description": f"## Background\n{title}",
                }
                for index, title in enumerate(titles)
            ],
            "throughput_trend": "improved",
            "latency_trend": "mixed",
            **extra,
        }
    )
