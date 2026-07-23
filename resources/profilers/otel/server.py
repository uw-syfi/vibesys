"""MCP tools for normalized servicebench OpenTelemetry reports."""

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator


class LatencyRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    count: StrictInt = Field(gt=0)
    error_count: StrictInt = Field(ge=0)
    mean_ms: StrictFloat = Field(ge=0)
    p50_ms: StrictFloat = Field(ge=0)
    p95_ms: StrictFloat = Field(ge=0)
    p99_ms: StrictFloat = Field(ge=0)
    max_ms: StrictFloat = Field(ge=0)

    @model_validator(mode="after")
    def validate_distribution(self) -> "LatencyRow":
        if self.error_count > self.count:
            raise ValueError("error_count must not exceed count")
        values = (self.mean_ms, self.p50_ms, self.p95_ms, self.p99_ms, self.max_ms)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("latency values must be finite")
        if not (self.p50_ms <= self.p95_ms <= self.p99_ms <= self.max_ms):
            raise ValueError("latency percentiles must be ordered")
        return self


class MeasurementWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str
    end: str

    @model_validator(mode="after")
    def validate_bounds(self) -> "MeasurementWindow":
        start = _parse_timestamp(self.start, "start")
        end = _parse_timestamp(self.end, "end")
        if end < start:
            raise ValueError("measurement window end must not precede start")
        return self


class TelemetryReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    source: str = Field(min_length=1)
    collected_at: str
    workload_name: str = Field(min_length=1)
    workload_hash: str = Field(min_length=1)
    measurement_windows: list[MeasurementWindow]
    span_count: StrictInt = Field(gt=0)
    error_count: StrictInt = Field(ge=0)
    services_by_p95: list[LatencyRow]
    spans_by_p95: list[LatencyRow]
    datastores_by_p95: list[LatencyRow] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_report(self) -> "TelemetryReport":
        _parse_timestamp(self.collected_at, "collected_at")
        if not self.measurement_windows:
            raise ValueError("measurement_windows must not be empty")
        if self.error_count > self.span_count:
            raise ValueError("error_count must not exceed span_count")
        for label, rows in (
            ("services_by_p95", self.services_by_p95),
            ("spans_by_p95", self.spans_by_p95),
            ("datastores_by_p95", self.datastores_by_p95),
        ):
            names = [row.name for row in rows]
            if len(names) != len(set(names)):
                raise ValueError(f"{label} contains duplicate names")
        if not self.services_by_p95 or not self.spans_by_p95:
            raise ValueError("services_by_p95 and spans_by_p95 must not be empty")
        return self


def load_report(path: str) -> TelemetryReport:
    return TelemetryReport.model_validate_json(Path(path).read_text(encoding="utf-8"))


def summarize_report(path: str, *, top: int = 10) -> dict:
    _validate_top(top)
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
    _validate_top(top)
    before = load_report(before_path)
    after = load_report(after_path)
    if _report_identity(before) != _report_identity(after):
        raise ValueError("reports must have matching workload identity and window count")
    return {
        "before_span_count": before.span_count,
        "after_span_count": after.span_count,
        "service_p95_changes": _compare_rows(before.services_by_p95, after.services_by_p95, top),
        "span_p95_changes": _compare_rows(before.spans_by_p95, after.spans_by_p95, top),
        "datastore_p95_changes": _compare_rows(
            before.datastores_by_p95, after.datastores_by_p95, top
        ),
    }


def _report_identity(report: TelemetryReport) -> tuple:
    return (
        report.workload_name,
        report.workload_hash,
        len(report.measurement_windows),
    )


def _parse_timestamp(value: str, label: str) -> datetime:
    if not value:
        raise ValueError(f"measurement window {label} must not be empty")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"measurement window {label} must be an RFC3339 timestamp") from exc
    if timestamp.tzinfo is None:
        raise ValueError(f"measurement window {label} must include a timezone")
    return timestamp


def _validate_top(top: int) -> None:
    if top <= 0:
        raise ValueError("top must be positive")


def _compare_rows(
    before_rows: list[LatencyRow], after_rows: list[LatencyRow], top: int
) -> list[dict]:
    before_by_name = {row.name: row for row in before_rows}
    after_by_name = {row.name: row for row in after_rows}
    # Report rows present in only one side too. Both reports carry only the
    # producer's top-N rows, so a row that entered or left the ranking is
    # exactly a large change; matching on the name intersection alone would
    # silently drop the biggest regressions and improvements.
    names = [row.name for row in after_rows]
    names += [row.name for row in before_rows if row.name not in after_by_name]
    changes = []
    for name in names:
        previous = before_by_name.get(name)
        current = after_by_name.get(name)
        before_p95 = previous.p95_ms if previous is not None else None
        after_p95 = current.p95_ms if current is not None else None
        delta = None
        delta_percent = None
        if before_p95 is not None and after_p95 is not None:
            delta = after_p95 - before_p95
            delta_percent = delta / before_p95 * 100 if before_p95 > 0 else None
        changes.append(
            {
                "name": name,
                "before_p95_ms": before_p95,
                "after_p95_ms": after_p95,
                "delta_p95_ms": delta,
                "delta_percent": delta_percent,
            }
        )
    changes.sort(key=_change_magnitude, reverse=True)
    return changes[:top]


def _change_magnitude(change: dict) -> float:
    # Rank matched rows by absolute p95 change; rank a row present in only one
    # report by its known p95 so a newly hot or newly absent span still surfaces.
    delta = change["delta_p95_ms"]
    if delta is not None:
        return abs(delta)
    for key in ("after_p95_ms", "before_p95_ms"):
        value = change[key]
        if value is not None:
            return value
    return 0.0


def find_reports(root: str = ".") -> list[str]:
    base = Path(root)
    reports = []
    for path in base.rglob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, RecursionError, ValueError):
            # Skip any file we cannot read or parse so one bad ``*.json`` under
            # the workspace cannot abort the whole scan. ValueError covers
            # non-JSON (JSONDecodeError), non-UTF-8 bytes (UnicodeDecodeError),
            # and oversized integer literals; RecursionError covers deeply
            # nested JSON. A candidate under evaluation controls workspace files,
            # so report discovery must tolerate hostile input.
            continue
        if isinstance(payload, dict) and payload.get("schema_version") == 1:
            try:
                TelemetryReport.model_validate(payload)
            except ValueError:
                continue
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
        """Compare p95 latency for services, spans, and datastores."""
        return compare_reports(before_path, after_path, top=top)

    return mcp


if __name__ == "__main__":
    build_server().run(transport="stdio")
