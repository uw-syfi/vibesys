"""Declarative lineage stress cases, projected as real core journal events."""

from __future__ import annotations

import json
from typing import Any

TITLES = (
    "Preallocate the arena",
    "Measure the hot path",
    "Separate the producer cursor",
    "Use relaxed loads on the fast path",
    "Isolate false sharing",
    "Batch the enqueue",
    "Park idle consumers",
    "Pad the slot headers",
    "Replace the mutex with a ticket lock",
    "Prefetch the next slot",
)


def lineage_fixture(declaration: dict[str, Any]) -> dict[str, Any]:
    """Expand count, chain and days; the shipping projector validates every generated event."""
    fixture = {key: value for (key, value) in declaration.items() if key != "lineage"}
    options = declaration["lineage"]
    count, chain, days = dimensions(options)
    events = list(fixture["events"])
    root = "flow:search/#1"

    def span(identity: str, parent: str | None, kind: str, **values: object) -> None:
        title, at, status, attrs = (values[key] for key in ("title", "at", "status", "attrs"))
        end, verdict, links = (values.get(key) for key in ("end", "verdict", "links"))
        body: dict[str, Any] = {
            "id": identity,
            "parent": parent,
            "type": "operation" if kind == "agent_turn" else "flow",
            "name": kind,
            "status": status,
            "started_at": at,
        }
        if end is not None:
            body["ended_at"] = end
        if kind != "search":
            body["label"] = {
                "kind": kind,
                "title": title,
                "attrs_json": json.dumps(attrs),
                "links": links or [],
            }
            if verdict is not None:
                body["label"]["verdict"] = verdict
        if kind == "agent_turn":
            body["invocation"] = identity
        events.append(
            {
                "sequence": len(events) + 1,
                "timestamp": "2026-10-15T16:30:51Z",
                "type": "span_started",
                "data": {"kind": "span_started", "span": body},
            }
        )

    span(root, None, "search", title="search", at=0, status="running", attrs={})

    def metric(value: float) -> list[dict[str, Any]]:
        return [{"name": "total_ops_per_sec", "value": value, "unit": "ops/s", "direction": "max"}]

    span(
        "flow:base/#1",
        root,
        "gate",
        end=50,
        verdict="passed",
        title="Baseline",
        at=0,
        status="done",
        attrs={
            "stage": "benchmark",
            "metric_value": 10000000.0,
            "vs_baseline": 1,
            "metrics": metric(10000000.0),
        },
    )
    span(
        "op:adopt",
        root,
        "adoption",
        title="best",
        at=0,
        status="running",
        attrs={"candidate": None},
    )
    passed: list[int] = []
    best = 1.0
    chained = {int(k * count / (chain + 1)) for k in range(chain)}
    for n in range(count):
        base = passed[-1] if passed else None
        outcome = (
            "running" if n >= count - 2 else "passed" if n in chained or n % 4 != 3 else "failed"
        )
        if n in chained:
            best += 3 / chain + 0.05
        ratio = best if n in chained else best * 0.8
        identity = f"flow:w{n}/#1"
        title = TITLES[n % len(TITLES)] + (f" {n}" if count > len(TITLES) else "")
        at = int(n * days * 86400 / count)
        done = outcome != "running"
        attrs = {"hypothesis_id": f"h{n}", "idea": f"Try: {title.lower()}"}
        if outcome == "passed":
            attrs["revision"] = f"c{n}"
            passed.append(n)
        span(
            identity,
            root,
            "workstream",
            end=at + 1800 if done else None,
            verdict=outcome if done else None,
            links=[] if base is None else [{"rel": "builds_on", "to": f"flow:w{base}/#1"}],
            title=title,
            at=at,
            status="done" if done else "running",
            attrs=attrs,
        )
        span(
            f"{identity}/turn",
            identity,
            "agent_turn",
            end=at + 900 if done else None,
            title="Implementer",
            at=at + 1,
            status="done" if done else "running",
            attrs={"role": "implementer", "agent": identity},
        )
        span(
            f"{identity}/gate",
            identity,
            "gate",
            end=at + 1700 if done else None,
            verdict="passed" if done else None,
            title="Benchmark",
            at=at + 910,
            status="done" if done else "running",
            attrs={
                "stage": "benchmark",
                "metric_value": ratio * 10000000.0,
                "vs_baseline": ratio,
                "metrics": metric(ratio * 10000000.0),
            },
        )
    fixture["events"] = events
    return fixture


def dimensions(options: dict[str, Any]) -> tuple[int, int, int]:
    if set(options) != {"count", "chain", "days"}:
        message = f"lineage: expected count, chain and days, got {sorted(options)}"
        raise ValueError(message)
    (count, chain, days) = (int(options[key]) for key in ("count", "chain", "days"))
    if not 1 <= chain <= count <= 600 or days < 1:
        message = "lineage: require 1 <= chain <= count <= 600 and positive days"
        raise ValueError(message)
    return count, chain, days
