"""Reproduce round CSV, SVG, and observations from saved measurement evidence.

Public interface: ``main(argv=None)`` and ``render_report(rows, destination,
metadata)``. Explicit round associations preserve framework round identity;
missing rows are gaps, never estimated performance. No MLX import is needed.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _metric_units() -> dict[str, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from evaluator import METRIC_SPECS

    return {name: str(spec["unit"]) for name, spec in METRIC_SPECS.items()}


class ReportRow(BaseModel):
    """One observed operating point or explicitly unmeasured round."""

    model_config = ConfigDict(extra="forbid", strict=True)
    round: int = Field(ge=0)
    status: Literal["baseline", "official", "provisional", "unreviewed", "missing", "failed"]
    source: str
    metrics: dict[str, float] = Field(default_factory=dict)
    diagnostics: dict[str, str] = Field(default_factory=dict)


class AgentMetadata(BaseModel):
    """Optional operator-supplied accounting; unavailable fields stay unknown."""

    model_config = ConfigDict(extra="forbid", strict=True)
    driving_models: list[str] = Field(default_factory=list)
    agent_wall_seconds: float | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    retries: int | None = Field(default=None, ge=0)
    observations: list[str] = Field(default_factory=list)


def _load_measurement(spec: str) -> ReportRow:
    round_text, separator, path_text = spec.partition(":")
    if not separator or not round_text.isdigit():
        raise ValueError("--measurement expects ROUND:PATH (baseline round is 0)")
    path = Path(path_text).resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from evaluator import SavedMeasurement

    measurement = SavedMeasurement.model_validate_json(path.read_text(encoding="utf-8"))
    round_number = int(round_text)
    if measurement.round is not None and round_number != measurement.round:
        raise ValueError(f"Round association contradicts {path}: {measurement.round}")
    if round_number == 0 and measurement.status != "baseline":
        raise ValueError(f"Round 0 must be an explicitly accepted baseline: {path}")
    status = "unreviewed" if measurement.status == "unassigned" else measurement.status
    return ReportRow(
        round=round_number,
        status=status,
        source=str(path),
        metrics=measurement.metrics,
        diagnostics=measurement.diagnostics,
    )


def _framework_metadata(project_root: Path | None, run_id: str | None) -> dict[str, Any]:
    if project_root is None:
        return {"availability": "not requested"}
    from vs_project.api import Project

    project = Project.open(project_root)
    # Both calls are public Project surfaces. Unsupported installed versions
    # fail explicitly; reconstructing .vibesys paths would bypass its contract.
    manifest = project.state.resolve_run(run_id)
    snapshot = project.state.portable_run_export(manifest.run_id)
    return {
        "availability": "available",
        "manifest": manifest.model_dump(mode="json"),
        "portable_state": [
            {"relative_path": str(item.relative_path), "contents": item.contents.decode("utf-8")}
            for item in snapshot.files
        ],
    }


def _svg(rows: list[ReportRow]) -> str:
    metrics = [name for name in _metric_units() if any(name in row.metrics for row in rows)]
    width, panel_height = 940, 235
    height = 65 + panel_height * len(metrics)
    x0, x1 = 165, 890
    min_round, max_round = min(row.round for row in rows), max(row.round for row in rows)

    def x(number: int) -> float:
        return (
            (x0 + x1) / 2
            if max_round == min_round
            else x0 + (number - min_round) * (x1 - x0) / (max_round - min_round)
        )

    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<g font-family="sans-serif" font-size="13" fill="#172033">',
        '<text x="25" y="25" font-size="18">'
        "Shared-prefix serving measurements by VibeSys round</text>",
        '<text x="25" y="47">Filled: baseline/official. Hollow: provisional/unreviewed. '
        "×: no measurement. No interpolation.</text>",
    ]
    for index, metric in enumerate(metrics):
        top = 80 + index * panel_height
        bottom = top + 145
        values = [row.metrics[metric] for row in rows if metric in row.metrics]
        ceiling = max(values) * 1.1 or 1.0
        label = html.escape(f"{metric} ({_metric_units()[metric]})")
        elements += [
            f'<text x="25" y="{top - 10}">{label}</text>',
            f'<path d="M{x0},{top}V{bottom}H{x1}" fill="none" stroke="#778196"/>',
        ]
        for fraction in (0, 0.5, 1):
            y = bottom - fraction * 145
            elements += [
                f'<text x="{x0 - 10}" y="{y + 4}" text-anchor="end">'
                f"{ceiling * fraction:.4g}</text>",
                f'<path d="M{x0},{y}H{x1}" stroke="#e2e6ed"/>',
            ]
        for row in rows:
            xpos = x(row.round)
            elements.append(
                f'<text x="{xpos}" y="{bottom + 20}" text-anchor="middle">{row.round}</text>'
            )
            if metric not in row.metrics:
                elements.append(
                    f'<text x="{xpos}" y="{bottom - 8}" text-anchor="middle" '
                    'fill="#b74738">×</text>'
                )
                continue
            y = bottom - row.metrics[metric] / ceiling * 145
            fill = "#2864bc" if row.status in {"baseline", "official"} else "white"
            title = html.escape(
                f"Round {row.round}; {row.status}; {row.metrics[metric]:.6g}; {row.source}"
            )
            elements.append(
                f'<circle cx="{xpos}" cy="{y}" r="5" fill="{fill}" '
                f'stroke="#2864bc" stroke-width="2"><title>{title}</title></circle>'
            )
        elements.append(
            f'<text x="{(x0 + x1) / 2}" y="{bottom + 42}" text-anchor="middle">VibeSys round</text>'
        )
    return "\n".join([*elements, "</g></svg>"])


def _writeup(rows: list[ReportRow], metadata: dict[str, Any]) -> str:
    accounting = AgentMetadata.model_validate(metadata["agent"])
    framework = metadata["framework"]
    manifest = framework.get("manifest", {})
    execution = manifest.get("execution", {})
    observed_models = [execution["model"]] if execution.get("model") else []
    observed_models.extend(
        policy["model"]
        for policy in execution.get("agent_roles", {}).values()
        if policy.get("model")
    )
    models = accounting.driving_models or sorted(set(observed_models))
    rounds = sorted({row.round for row in rows if row.round > 0 and row.status != "missing"})
    lines = [
        "# Shared-prefix serving experiment",
        "",
        "Served model: mlx-community/Llama-3.2-3B-Instruct-4bit "
        "(the cached pinned revision in reference/meta.json).",
        "Driving models: "
        f"{', '.join(models) if models else 'unavailable; no driving-model evidence supplied'}.",
        f"Default reasoning effort: {execution.get('default_reasoning_effort') or 'unavailable'}.",
        f"Optimization rounds with supplied evidence/status: {len(rounds)} "
        f"({', '.join(map(str, rounds)) or 'none'}).",
        "Round numbers are explicit associations with saved measurements. "
        "They are not inferred from file order.",
        "",
        "## Agent accounting",
        "",
    ]
    for label, value in (
        ("Agent wall seconds", accounting.agent_wall_seconds),
        ("Input tokens", accounting.input_tokens),
        ("Output tokens", accounting.output_tokens),
        ("Retries", accounting.retries),
    ):
        lines.append(f"- {label}: {value if value is not None else 'unavailable'}.")
    lines += [
        "",
        "## Measurement observations",
        "",
        "The operating point is four concurrent streamed requests after one correct warmup "
        "on a fresh server.",
        "Stock prefix caching is part of round zero. Four responses are a smoke baseline; "
        "small differences require repeated matched-seed batches.",
        "RSS is the owned server process high-water mark including startup and warmup, "
        "not total unified-memory or Metal allocation.",
        "Unmeasured, failed, and unreviewed rounds remain explicit. "
        "Optional telemetry is omitted when unavailable.",
        "",
    ]
    for row in rows:
        lines.append(f"- Round {row.round}: {row.status}; source: `{row.source}`.")
        for metric, reason in sorted(row.diagnostics.items()):
            lines.append(f"  - {metric}: {reason}")
    lines += [
        "",
        *accounting.observations,
        "",
        f"Framework metadata: {framework['availability']}. "
        "Full supplied metadata is retained in report-metadata.json.",
    ]
    return "\n".join(lines) + "\n"


def render_report(rows: list[ReportRow], destination: Path, metadata: dict[str, Any]) -> None:
    """Write reviewable artifacts exclusively; all metric values come from evidence."""
    if not rows:
        raise ValueError("At least one real measurement is required")
    if len({row.round for row in rows}) != len(rows):
        raise ValueError("Duplicate round: report matched repeats separately")
    rows = sorted(rows, key=lambda row: row.round)
    for row in rows:
        for name, value in row.metrics.items():
            if name not in _metric_units() or not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid metric {name}: {value}")
        if row.status in {"failed", "missing"} and row.metrics:
            raise ValueError(f"Unmeasured round {row.round} cannot have metrics")
        if row.round == 0 and row.status not in {"baseline", "missing", "failed"}:
            raise ValueError("Round 0 must be an explicitly accepted baseline")
    present = {row.round for row in rows}
    for number in range(rows[-1].round + 1):
        if number not in present:
            rows.append(ReportRow(round=number, status="missing", source="no evidence supplied"))
    rows.sort(key=lambda row: row.round)
    graph = _svg(rows)
    writeup = _writeup(rows, metadata)
    metadata_json = json.dumps(metadata, indent=2, allow_nan=False) + "\n"
    destination.mkdir(parents=True, exist_ok=False)
    with (destination / "rounds.csv").open("x", encoding="utf-8", newline="") as stream:
        columns = ["round", "status", "source", *_metric_units()]
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {"round": row.round, "status": row.status, "source": row.source, **row.metrics}
            )
    (destination / "performance.svg").write_text(graph, encoding="utf-8")
    (destination / "writeup.md").write_text(writeup, encoding="utf-8")
    (destination / "report-metadata.json").write_text(metadata_json, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Associate measured evidence with actual rounds, then reproduce the report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--measurement", action="append", required=True, metavar="ROUND:PATH")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--missing-round", action="append", default=[], metavar="ROUND:STATUS")
    parser.add_argument(
        "--agent-metadata", type=Path, help="Optional JSON AgentMetadata accounting."
    )
    parser.add_argument("--project-root", type=Path, help="Optional framework Project root.")
    parser.add_argument("--run-id", help="Framework run identity, resolved through Project.")
    args = parser.parse_args(argv)
    if args.run_id and not args.project_root:
        parser.error("--run-id requires --project-root")
    try:
        rows = [_load_measurement(spec) for spec in args.measurement]
        for spec in args.missing_round:
            number, separator, status = spec.partition(":")
            if (
                not separator
                or not number.isdigit()
                or status not in {"missing", "failed", "unreviewed"}
            ):
                raise ValueError("--missing-round expects ROUND:missing|failed|unreviewed")
            rows.append(
                ReportRow(
                    round=int(number), status=status, source="operator-associated unmeasured round"
                )
            )
        agent = (
            AgentMetadata.model_validate_json(args.agent_metadata.read_text(encoding="utf-8"))
            if args.agent_metadata
            else AgentMetadata()
        )
        metadata = {
            "agent": agent.model_dump(),
            "framework": _framework_metadata(args.project_root, args.run_id),
            "command": [sys.executable, str(Path(__file__).resolve()), *(argv or sys.argv[1:])],
            "cwd": str(Path.cwd()),
        }
        render_report(rows, args.output_dir, metadata)
    except (OSError, ValueError, ImportError, AttributeError, RuntimeError) as exc:
        parser.exit(1, f"Report failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
