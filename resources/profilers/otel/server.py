"""MCP tools for normalized servicebench OpenTelemetry reports."""

import json
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field


class LatencyRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    count: int = Field(gt=0)
    error_count: int = Field(ge=0)
    mean_ms: float = Field(ge=0)
    p50_ms: float = Field(ge=0)
    p95_ms: float = Field(ge=0)
    p99_ms: float = Field(ge=0)
    max_ms: float = Field(ge=0)


class MeasurementWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str
    end: str


class TelemetryReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    source: str
    collected_at: str
    workload_name: str
    workload_hash: str
    measurement_windows: list[MeasurementWindow]
    span_count: int = Field(gt=0)
    error_count: int = Field(ge=0)
    services_by_p95: list[LatencyRow]
    spans_by_p95: list[LatencyRow]
    datastores_by_p95: list[LatencyRow] = Field(default_factory=list)


def load_report(path: str) -> TelemetryReport:
    return TelemetryReport.model_validate_json(Path(path).read_text(encoding="utf-8"))


def summarize_report(path: str, *, top: int = 10) -> dict:
    report = load_report(path)
    return {
        "source": report.source,
        "collected_at": report.collected_at,
        "workload_name": report.workload_name,
        "workload_hash": report.workload_hash,
        "measurement_windows": [window.model_dump() for window in report.measurement_windows],
        "span_count": report.span_count,
        "error_count": report.error_count,
        "services_by_p95": [row.model_dump() for row in report.services_by_p95[:top]],
        "spans_by_p95": [row.model_dump() for row in report.spans_by_p95[:top]],
        "datastores_by_p95": [row.model_dump() for row in report.datastores_by_p95[:top]],
    }


def compare_reports(before_path: str, after_path: str, *, top: int = 10) -> dict:
    before = load_report(before_path)
    after = load_report(after_path)
    before_rows = {row.name: row for row in before.services_by_p95}
    changes = []
    for row in after.services_by_p95:
        previous = before_rows.get(row.name)
        if previous is None:
            continue
        changes.append(
            {
                "name": row.name,
                "before_p95_ms": previous.p95_ms,
                "after_p95_ms": row.p95_ms,
                "delta_p95_ms": row.p95_ms - previous.p95_ms,
                "delta_percent": (
                    (row.p95_ms - previous.p95_ms) / previous.p95_ms * 100
                    if previous.p95_ms > 0
                    else None
                ),
            }
        )
    changes.sort(key=lambda item: abs(item["delta_p95_ms"]), reverse=True)
    return {
        "before_span_count": before.span_count,
        "after_span_count": after.span_count,
        "service_p95_changes": changes[:top],
    }


def find_reports(root: str = ".") -> list[str]:
    base = Path(root)
    reports = []
    for path in base.rglob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("schema_version") == 1:
            if "services_by_p95" in payload and "spans_by_p95" in payload:
                reports.append(path.as_posix())
    return sorted(reports)


def build_server() -> FastMCP:
    mcp = FastMCP("vibesys-otel-profiler")

    @mcp.tool()
    def reports(root: str = ".") -> list[str]:
        """Find normalized servicebench OTel reports below a workspace path."""
        return find_reports(root)

    @mcp.tool()
    def summary(path: str, top: int = 10) -> dict:
        """Return ranked service, span, and datastore latency evidence."""
        return summarize_report(path, top=top)

    @mcp.tool()
    def compare(before_path: str, after_path: str, top: int = 10) -> dict:
        """Compare service p95 latency between two normalized OTel reports."""
        return compare_reports(before_path, after_path, top=top)

    return mcp


if __name__ == "__main__":
    build_server().run(transport="stdio")
