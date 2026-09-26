"""Tests for the profiler MCP servers.

We verify tool registration via ``FastMCP.list_tools`` and exercise a few
tools end-to-end through ``FastMCP.call_tool``. The stdio JSON-RPC framing
itself is the ``mcp`` package's responsibility.
"""

import asyncio
import contextlib
import importlib.util
import json
import os
import select
import sqlite3
import sys
import textwrap
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from pathlib import Path
from types import ModuleType
from typing import Protocol

import pytest

from vibesys.profilers import ProfilerKind, mcp_spec


class _ToolInfo(Protocol):
    name: str


class _McpServer(Protocol):
    async def list_tools(self) -> Sequence[_ToolInfo]: ...
    async def call_tool(
        self, name: str, arguments: dict[str, object]
    ) -> tuple[Sequence[object], dict[str, object]]: ...


# The servers live under resources/ (co-located with the analysis scripts) so
# importing them by file path keeps the tests decoupled from sys.path state.
def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    # Inject the server's parent dir onto sys.path BEFORE exec so the
    # server's ``import analyze_nsys`` / ``import analyze_torch_profile``
    # succeeds.
    parent = str(path.parent)
    inserted = False
    if parent not in sys.path:
        sys.path.insert(0, parent)
        inserted = True
    try:
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        if inserted:
            sys.path.remove(parent)
    return module


_REPO = Path(__file__).resolve().parents[3]


def test_profiler_mcp_spec_maps_known_kinds_exactly() -> None:
    assert mcp_spec(ProfilerKind.NONE) is None

    nsys = mcp_spec(ProfilerKind.NSYS)
    assert nsys is not None
    assert nsys.name == "vibesys-nsys-profiler"
    assert nsys.args == ("nsys_profiler/server.py",)

    rocprof = mcp_spec(ProfilerKind.ROCPROF)
    assert rocprof is not None
    assert rocprof.name == "vibesys-rocprof-profiler"
    assert rocprof.args == ("rocprof_profiler/server.py",)

    torch = mcp_spec(ProfilerKind.TORCH)
    assert torch is not None
    assert torch.name == "vibesys-torch-profiler"
    assert torch.args == ("torch_profiler/server.py",)

    neuron = mcp_spec(ProfilerKind.NEURON)
    assert neuron is not None
    assert neuron.name == "vibesys-neuron-profiler"
    assert neuron.args == ("neuron_profiler/server.py",)

    otel = mcp_spec(ProfilerKind.OTEL)
    assert otel is not None
    assert otel.name == "vibesys-otel-profiler"
    assert otel.args == ("otel_profiler/server.py",)

    macos = mcp_spec(ProfilerKind.MACOS_CPU)
    assert macos is not None
    assert macos.name == "vibesys-macos-cpu-profiler"
    assert macos.args == ("macos_cpu_profiler/server.py",)


def test_profiler_mcp_spec_rejects_unknown_kind() -> None:
    # The rejection is a runtime guard against a value the annotation forbids,
    # so route the bad argument through an untyped mapping.
    invalid_kwargs: dict = {"profiler_kind": "bogus"}
    with pytest.raises(TypeError, match="ProfilerKind"):
        mcp_spec(**invalid_kwargs)


@pytest.fixture(scope="module")
def nsys_server_mod() -> ModuleType:
    return _load_module(
        "_nsys_server",
        _REPO / "resources" / "profilers" / "nsys" / "server.py",
    )


@pytest.fixture(scope="module")
def torch_server_mod() -> ModuleType:
    return _load_module(
        "_torch_server",
        _REPO / "resources" / "profilers" / "torch" / "server.py",
    )


@pytest.fixture(scope="module")
def otel_server_mod() -> ModuleType:
    return _load_module(
        "_otel_server",
        _REPO / "resources" / "profilers" / "otel" / "server.py",
    )


@pytest.fixture(scope="module")
def headroom_server_mod() -> ModuleType:
    return _load_module(
        "_headroom_server",
        _REPO / "resources" / "profilers" / "headroom" / "server.py",
    )


@pytest.fixture(scope="module")
def rocprof_server_mod() -> ModuleType:
    return _load_module(
        "_rocprof_server",
        _REPO / "resources" / "profilers" / "rocprof" / "server.py",
    )


async def _list_tool_names(server: _McpServer) -> set[str]:
    tools = await server.list_tools()
    return {t.name for t in tools}


async def _call_tool(server: _McpServer, name: str, **kwargs: object) -> str:
    _, structured = await server.call_tool(name, kwargs)
    result = structured["result"]
    if not isinstance(result, str):
        raise TypeError
    return result


async def _call_structured_tool(
    server: _McpServer, name: str, **kwargs: object
) -> dict[str, object]:
    _, structured = await server.call_tool(name, kwargs)
    return structured


def _json_value_at(value: object, *path: str | int) -> object:
    """Read a JSON path while checking each container at the contract boundary."""
    current = value
    for key in path:
        if isinstance(current, dict):
            assert isinstance(key, str)
            current = current[key]
        elif isinstance(current, list):
            assert isinstance(key, int)
            current = current[key]
        else:
            raise TypeError
    return current


# ---------------------------------------------------------------------------
# nsys MCP server
# ---------------------------------------------------------------------------


class TestNsysMcpServer:
    def test_registers_expected_tools(self, nsys_server_mod: ModuleType) -> None:
        server = nsys_server_mod.build_server()
        names = asyncio.run(_list_tool_names(server))
        assert names == {
            "export",
            "tables",
            "kernels",
            "cpu_overhead",
            "idle_gaps",
            "memory",
            "graph_replays",
            "step_timeline",
            "query",
            "summary",
        }

    def test_tables_tool_reports_empty_db(
        self, nsys_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        """Against an empty SQLite file, ``tables`` returns a no-output marker."""
        db = tmp_path / "empty.sqlite"
        sqlite3.connect(str(db)).close()

        server = nsys_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "tables", report=str(db)))
        # No tables → the cmd_tables body prints nothing; the wrapper
        # coerces that to "(no output)".
        assert out == "(no output)"

    def test_kernels_tool_reports_no_data(
        self, nsys_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        """A SQLite file without a CUPTI_ACTIVITY_KIND_KERNEL table returns a friendly message."""
        db = tmp_path / "nokernels.sqlite"
        sqlite3.connect(str(db)).close()

        server = nsys_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "kernels", report=str(db)))
        assert "No kernel data" in out

    def test_query_tool_runs_arbitrary_sql(
        self, nsys_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        db = tmp_path / "q.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE t (x INTEGER, y TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b')")
        conn.commit()
        conn.close()

        server = nsys_server_mod.build_server()
        out = asyncio.run(
            _call_tool(server, "query", report=str(db), sql="SELECT * FROM t ORDER BY x"),
        )
        assert "x\ty" in out
        assert "1\ta" in out
        assert "2\tb" in out


# ---------------------------------------------------------------------------
# OpenTelemetry MCP server
# ---------------------------------------------------------------------------


class TestOtelMcpServer:
    def test_registers_expected_tools(self, otel_server_mod: ModuleType) -> None:
        server = otel_server_mod.build_server()
        names = asyncio.run(_list_tool_names(server))
        assert names == {
            "reports",
            "summary",
            "compare",
            "trace_graphs",
            "critical_path",
            "trace_breakdown",
        }

    def test_discovers_and_summarizes_critical_path(
        self, otel_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        report_path = tmp_path / "telemetry.json"
        graph_path = tmp_path / "trace-graph.json"
        report_path.write_text(json.dumps(_otel_report(20.0)))
        graph_path.write_text(json.dumps(_trace_graph()))

        summary = otel_server_mod.summarize_critical_path(str(graph_path), str(report_path), top=1)

        assert otel_server_mod.find_reports(str(tmp_path)) == [report_path.as_posix()]
        assert otel_server_mod.find_trace_graphs(str(tmp_path)) == [graph_path.as_posix()]
        assert summary.workload_name == "hotel"
        assert summary.quality.eligible_traces == 3
        assert len(summary.roots) == 1
        assert summary.omitted_root_count == 0
        root = summary.roots[0]
        assert root.algorithm == "wall_clock_active_leaf_v1"
        assert root.scope == "synchronous_request"
        assert root.nodes_by_contribution[0].operation == "Search/Nearby"
        assert root.omitted_contributor_count == 1
        assert root.representative.segments[0].node_id == "node-001"
        assert root.representative.omitted_segment_count == 1

    def test_critical_path_tool_returns_structured_summary(
        self, otel_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        graph_path = tmp_path / "trace-graph.json"
        report_path = tmp_path / "telemetry.json"
        graph_path.write_text(json.dumps(_trace_graph()))
        report_path.write_text(json.dumps(_otel_report(20.0)))

        server = otel_server_mod.build_server()
        result = asyncio.run(
            _call_structured_tool(
                server,
                "critical_path",
                path=str(graph_path),
                telemetry_path=str(report_path),
                top=1,
            )
        )

        assert result["workload_name"] == "hotel"
        assert _json_value_at(result, "roots", 0, "nodes_by_contribution", 0, "service") == "search"

    def test_trace_breakdown_returns_call_graph_and_waterfall(
        self, otel_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        graph_path = tmp_path / "trace-graph.json"
        report_path = tmp_path / "telemetry.json"
        graph_path.write_text(json.dumps(_trace_graph()))
        report_path.write_text(json.dumps(_otel_report(20.0)))

        breakdown = otel_server_mod.summarize_trace_breakdown(
            str(graph_path), str(report_path), top=10
        )

        root = breakdown.roots[0]
        assert [(node.node_id, node.depth) for node in root.nodes] == [
            ("node-001", 0),
            ("node-002", 1),
        ]
        assert root.nodes[0].inclusive_latency_ms.mean_ms == 20.0
        assert root.nodes[0].exclusive_latency_ms.mean_ms == 5.0
        assert (root.omitted_node_count, root.omitted_edge_count) == (0, 0)
        assert [edge.from_node for edge in root.edges] == ["node-001"]
        waterfall = root.representative_trace
        assert [(span.offset_ms, span.duration_ms) for span in waterfall.spans] == [
            (0.0, 20.0),
            (5.0, 15.0),
        ]
        assert waterfall.omitted_span_count == 0

    def test_trace_breakdown_bounds_output_and_keeps_the_costly_nodes(
        self, otel_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        graph_path = tmp_path / "trace-graph.json"
        report_path = tmp_path / "telemetry.json"
        graph_path.write_text(json.dumps(_trace_graph()))
        report_path.write_text(json.dumps(_otel_report(20.0)))

        breakdown = otel_server_mod.summarize_trace_breakdown(
            str(graph_path), str(report_path), top=1
        )

        root = breakdown.roots[0]
        # Ranking is by inclusive p95, so the cheap leaf is what gets dropped,
        # and an edge to a dropped node cannot dangle.
        assert [node.node_id for node in root.nodes] == ["node-001"]
        assert (root.omitted_node_count, root.omitted_edge_count) == (1, 1)
        assert root.edges == []
        assert [span.duration_ms for span in root.representative_trace.spans] == [20.0]
        assert root.representative_trace.omitted_span_count == 1

    def test_trace_breakdown_tool_rejects_a_graph_from_another_workload(
        self, otel_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        graph_path = tmp_path / "trace-graph.json"
        report_path = tmp_path / "telemetry.json"
        graph = _trace_graph()
        graph["workload_hash"] = "def456"
        graph_path.write_text(json.dumps(graph))
        report_path.write_text(json.dumps(_otel_report(20.0)))

        with pytest.raises(ValueError, match="matching workload identity and windows"):
            otel_server_mod.summarize_trace_breakdown(str(graph_path), str(report_path))

    def test_trace_breakdown_tool_returns_structured_summary(
        self, otel_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        graph_path = tmp_path / "trace-graph.json"
        report_path = tmp_path / "telemetry.json"
        graph_path.write_text(json.dumps(_trace_graph()))
        report_path.write_text(json.dumps(_otel_report(20.0)))

        server = otel_server_mod.build_server()
        result = asyncio.run(
            _call_structured_tool(
                server,
                "trace_breakdown",
                path=str(graph_path),
                telemetry_path=str(report_path),
                top=10,
            )
        )

        assert result["workload_name"] == "hotel"
        assert _json_value_at(result, "roots", 0, "representative_trace", "trace_id") == "trace-a"
        assert _json_value_at(result, "roots", 0, "nodes", 1, "service") == "search"

    def test_critical_path_rejects_graph_from_another_measurement_window(
        self, otel_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        graph_path = tmp_path / "trace-graph.json"
        report_path = tmp_path / "telemetry.json"
        graph = _trace_graph()
        graph["measurement_windows"][0]["start"] = "2026-07-23T12:00:00Z"
        graph["measurement_windows"][0]["end"] = "2026-07-23T12:00:01Z"
        graph_path.write_text(json.dumps(graph))
        report_path.write_text(json.dumps(_otel_report(20.0)))

        with pytest.raises(ValueError, match="matching workload identity and windows"):
            otel_server_mod.summarize_critical_path(str(graph_path), str(report_path))

    def test_load_trace_graph_rejects_invalid_critical_path_geometry(
        self, otel_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        graph = _trace_graph()
        graph["roots"][0]["critical_path"]["representative"]["segments"][1]["offset_ms"] = 0.0
        graph_path = tmp_path / "invalid-trace-graph.json"
        graph_path.write_text(json.dumps(graph))

        with pytest.raises(ValueError, match="contiguous"):
            otel_server_mod.load_trace_graph(str(graph_path))

    def test_find_trace_graphs_skips_hostile_and_malformed_json(
        self, otel_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        valid = tmp_path / "valid-graph.json"
        valid.write_text(json.dumps(_trace_graph()))
        malformed = _trace_graph()
        malformed["quality"]["eligible_traces"] = 99
        (tmp_path / "malformed-graph.json").write_text(json.dumps(malformed))
        (tmp_path / "binary.json").write_bytes(b"\xff\xfe\x00\x01 not valid utf-8")
        (tmp_path / "nested.json").write_text("[" * 3000 + "]" * 3000)

        assert otel_server_mod.find_trace_graphs(str(tmp_path)) == [valid.as_posix()]

    def test_summary_and_compare_use_normalized_service_rows(
        self, otel_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        before = tmp_path / "before.json"
        after = tmp_path / "after.json"
        before.write_text(json.dumps(_otel_report(20.0)))
        after.write_text(json.dumps(_otel_report(12.0)))

        summary = otel_server_mod.summarize_report(str(after))
        comparison = otel_server_mod.compare_reports(str(before), str(after))

        assert summary.span_count == 4
        assert summary.services_by_p95[0].name == "frontend"
        assert comparison.service_p95_changes[0].model_dump() == {
            "name": "frontend",
            "before_p95_ms": 20.0,
            "after_p95_ms": 12.0,
            "delta_p95_ms": -8.0,
            "delta_percent": -40.0,
        }
        assert comparison.span_p95_changes[0].name == "frontend:GET /hotels"
        assert comparison.span_p95_changes[0].delta_p95_ms == -8.0
        assert comparison.datastore_p95_changes[0].name == "frontend:db"
        assert comparison.datastore_p95_changes[0].delta_p95_ms == -8.0
        assert otel_server_mod.find_reports(str(tmp_path)) == [
            after.as_posix(),
            before.as_posix(),
        ]

    def test_accepts_report_produced_by_go_otelcapture(self, otel_server_mod: ModuleType) -> None:
        """Round-trip a report generated by the Go otelcapture binary.

        Pins the cross-language contract: the JSON the evaluator writes must
        keep parsing under the Pydantic model without loosening validation.
        The fixture was produced by ``cmd/otelcapture`` from synthetic OTLP
        NDJSON (see the PR adding it for the generation recipe).
        """
        fixture = _REPO / "tests" / "support" / "data" / "otelcapture_hotel_report.json"

        report = otel_server_mod.load_report(str(fixture))
        summary = otel_server_mod.summarize_report(str(fixture))

        assert report.workload_name == "hotel-reservation"
        assert summary.span_count == 9
        assert summary.error_count == 1
        assert [row.name for row in summary.services_by_p95] == [
            "frontend",
            "search",
            "geo",
            "profile",
        ]
        assert summary.services_by_p95[0].error_count == 1
        assert summary.datastores_by_p95[0].name == "profile:memcached_get_profile"
        assert otel_server_mod.find_reports(str(fixture.parent)) == [fixture.as_posix()]

    @pytest.mark.parametrize("identity_field", ["workload_name", "workload_hash"])
    def test_compare_rejects_incompatible_reports(
        self, otel_server_mod: ModuleType, tmp_path: Path, identity_field: str
    ) -> None:
        before = tmp_path / "before.json"
        after = tmp_path / "after.json"
        before.write_text(json.dumps(_otel_report(20.0)))
        after_report = _otel_report(12.0)
        after_report[identity_field] = "different"
        after.write_text(json.dumps(after_report))

        with pytest.raises(ValueError, match="matching workload identity"):
            otel_server_mod.compare_reports(str(before), str(after))

    def test_compare_allows_run_specific_measurement_timestamps(
        self, otel_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        before = tmp_path / "before.json"
        after = tmp_path / "after.json"
        before.write_text(json.dumps(_otel_report(20.0)))
        after_report = _otel_report(12.0)
        after_report["measurement_windows"] = [
            {"start": "2026-07-23T12:00:00Z", "end": "2026-07-23T12:00:01Z"}
        ]
        after.write_text(json.dumps(after_report))

        comparison = otel_server_mod.compare_reports(str(before), str(after))

        assert comparison.service_p95_changes[0].delta_p95_ms == -8.0

    def test_load_report_rejects_invalid_aggregate_error_count(
        self, otel_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        report = _otel_report(20.0)
        report["span_count"] = 1
        report["error_count"] = 2
        path = tmp_path / "invalid.json"
        path.write_text(json.dumps(report))

        with pytest.raises(ValueError, match="error_count"):
            otel_server_mod.load_report(str(path))

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda report: report.update({"workload_name": ""}),
            lambda report: report.update({"services_by_p95": []}),
            lambda report: report["spans_by_p95"].append(report["spans_by_p95"][0]),
            lambda report: report["measurement_windows"].__setitem__(
                0, {"start": "not-a-timestamp", "end": "2026-07-22T12:00:01Z"}
            ),
            lambda report: report["services_by_p95"][0].update({"p50_ms": 999.0}),
            lambda report: report["services_by_p95"][0].update({"mean_ms": -1.0}),
            lambda report: report["services_by_p95"][0].update({"p99_ms": float("inf")}),
        ],
        ids=[
            "empty-identity",
            "empty-services",
            "duplicate-spans",
            "invalid-window",
            "unordered-percentiles",
            "negative-latency",
            "non-finite-latency",
        ],
    )
    def test_load_report_rejects_malformed_contract(
        self,
        otel_server_mod: ModuleType,
        tmp_path: Path,
        mutate: Callable[[dict[str, object]], None],
    ) -> None:
        report = _otel_report(20.0)
        mutate(report)
        path = tmp_path / "invalid.json"
        path.write_text(json.dumps(report))

        with pytest.raises(ValueError, match=r"\S"):
            otel_server_mod.load_report(str(path))

    def test_summary_rejects_non_positive_top(
        self, otel_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        path = tmp_path / "report.json"
        path.write_text(json.dumps(_otel_report(20.0)))

        with pytest.raises(ValueError, match="top must be positive"):
            otel_server_mod.summarize_report(str(path), top=0)

    def test_compare_rejects_non_positive_top_before_reading_files(
        self, otel_server_mod: ModuleType
    ) -> None:
        # top is validated before any file I/O, so unreadable paths do not matter.
        with pytest.raises(ValueError, match="top must be positive"):
            otel_server_mod.compare_reports("missing-before.json", "missing-after.json", top=0)

    def test_find_reports_skips_hostile_json(
        self, otel_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        valid = tmp_path / "valid.json"
        valid.write_text(json.dumps(_otel_report(20.0)))
        # A candidate under evaluation controls workspace files; none of these
        # may abort discovery of the valid report.
        (tmp_path / "binary.json").write_bytes(b"\xff\xfe\x00\x01 not valid utf-8")
        (tmp_path / "nested.json").write_text("[" * 3000 + "]" * 3000)
        (tmp_path / "bigint.json").write_text('{"schema_version": ' + "9" * 5000 + "}")
        (tmp_path / "truncated.json").write_text('{"schema_version": 1')

        assert otel_server_mod.find_reports(str(tmp_path)) == [valid.as_posix()]

    def test_compare_surfaces_rows_present_in_one_report(
        self, otel_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        def service_row(name: str, p95: float) -> dict:
            return {
                "name": name,
                "count": 4,
                "error_count": 0,
                "mean_ms": p95 - 3,
                "p50_ms": p95 - 5,
                "p95_ms": p95,
                "p99_ms": p95 + 1,
                "max_ms": p95 + 2,
            }

        before_report = _otel_report(20.0)
        before_report["services_by_p95"] = [
            service_row("frontend", 10.0),
            service_row("checkout", 8.0),
        ]
        after_report = _otel_report(20.0)
        after_report["services_by_p95"] = [
            service_row("frontend", 10.0),
            service_row("newsvc", 500.0),
        ]

        before = tmp_path / "before.json"
        after = tmp_path / "after.json"
        before.write_text(json.dumps(before_report))
        after.write_text(json.dumps(after_report))

        changes = otel_server_mod.compare_reports(str(before), str(after)).service_p95_changes
        by_name = {change.name: change for change in changes}

        assert set(by_name) == {"frontend", "checkout", "newsvc"}
        # A row new to the after report is surfaced with no baseline...
        assert by_name["newsvc"].before_p95_ms is None
        assert by_name["newsvc"].after_p95_ms == 500.0
        assert by_name["newsvc"].delta_p95_ms is None
        assert by_name["newsvc"].delta_percent is None
        # ...and a row absent from the after ranking is surfaced too.
        assert by_name["checkout"].before_p95_ms == 8.0
        assert by_name["checkout"].after_p95_ms is None
        # The largest-magnitude row ranks first even without a delta.
        assert changes[0].name == "newsvc"
        assert by_name["frontend"].delta_p95_ms == 0.0


def _otel_report(p95: float) -> dict:
    row = {
        "name": "frontend",
        "count": 4,
        "error_count": 0,
        "mean_ms": p95 - 3,
        "p50_ms": p95 - 5,
        "p95_ms": p95,
        "p99_ms": p95 + 1,
        "max_ms": p95 + 2,
    }
    span = {**row, "name": "frontend:GET /hotels"}
    datastore = {**row, "name": "frontend:db"}
    return {
        "schema_version": 1,
        "source": "otlp-json",
        "collected_at": "2026-07-22T12:00:00Z",
        "workload_name": "hotel",
        "workload_hash": "abc123",
        "measurement_windows": [{"start": "2026-07-22T12:00:00Z", "end": "2026-07-22T12:00:01Z"}],
        "span_count": 4,
        "error_count": 0,
        "services_by_p95": [row],
        "spans_by_p95": [span],
        "datastores_by_p95": [datastore],
    }


def _trace_graph() -> dict:
    def distribution(mean: float, *, count: int = 3) -> dict:
        return {
            "count": count,
            "error_count": 0,
            "mean_ms": mean,
            "p50_ms": mean,
            "p95_ms": mean + 1.0,
            "p99_ms": mean + 2.0,
            "max_ms": mean + 3.0,
        }

    return {
        "schema_version": 2,
        "source": "otlp-json",
        "collected_at": "2026-07-22T12:00:00Z",
        "workload_name": "hotel",
        "workload_hash": "abc123",
        "measurement_windows": [{"start": "2026-07-22T12:00:00Z", "end": "2026-07-22T12:00:01Z"}],
        "quality": {
            "captured_traces": 3,
            "eligible_traces": 3,
            "excluded_traces": 0,
            "matched_client_server_pairs": 3,
            "unmatched_client_spans": 0,
            "unmatched_server_spans": 0,
            "async_relationships": 1,
        },
        "trials": [{"trial": 1, "captured_traces": 3, "eligible_traces": 3, "excluded_traces": 0}],
        "roots": [
            {
                "service": "frontend",
                "operation": "GET /hotels",
                "trace_count": 3,
                "error_count": 0,
                "latency_ms": distribution(20.0),
                "nodes": [
                    {
                        "id": "node-001",
                        "path": "frontend:GET /hotels",
                        "service": "frontend",
                        "operation": "GET /hotels",
                        "kind": "server",
                        "inclusive_latency_ms": distribution(20.0),
                        "exclusive_latency_ms": distribution(5.0),
                    },
                    {
                        "id": "node-002",
                        "path": "frontend:GET /hotels > search:Search/Nearby",
                        "service": "search",
                        "operation": "Search/Nearby",
                        "kind": "server",
                        "inclusive_latency_ms": distribution(15.0),
                        "exclusive_latency_ms": distribution(15.0),
                    },
                ],
                "edges": [
                    {"from": "node-001", "to": "node-002", "relationship": "child", "count": 3}
                ],
                "representative_trace": {
                    "trace_id": "trace-a",
                    "duration_ms": 20.0,
                    "spans": [
                        {
                            "node_id": "node-001",
                            "service": "frontend",
                            "operation": "GET /hotels",
                            "offset_ms": 0.0,
                            "duration_ms": 20.0,
                        },
                        {
                            "node_id": "node-002",
                            "service": "search",
                            "operation": "Search/Nearby",
                            "offset_ms": 5.0,
                            "duration_ms": 15.0,
                        },
                    ],
                },
                "critical_path": {
                    "algorithm": "wall_clock_active_leaf_v1",
                    "scope": "synchronous_request",
                    "trace_count": 3,
                    "async_relationships_excluded": 1,
                    "duration_ms": distribution(20.0),
                    "nodes_by_contribution": [
                        {
                            "node_id": "node-002",
                            "path": "frontend:GET /hotels > search:Search/Nearby",
                            "service": "search",
                            "operation": "Search/Nearby",
                            "contribution_ms": distribution(15.0),
                        },
                        {
                            "node_id": "node-001",
                            "path": "frontend:GET /hotels",
                            "service": "frontend",
                            "operation": "GET /hotels",
                            "contribution_ms": distribution(5.0),
                        },
                    ],
                    "representative": {
                        "trace_id": "trace-a",
                        "duration_ms": 20.0,
                        "segments": [
                            {"node_id": "node-001", "offset_ms": 0.0, "duration_ms": 5.0},
                            {"node_id": "node-002", "offset_ms": 5.0, "duration_ms": 15.0},
                        ],
                    },
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# torch MCP server
# ---------------------------------------------------------------------------


def _chrome_trace_events(  # noqa: ANN202  # LW-910183; this private helper's return type is intentionally left loose; annotating it now is separate cleanup work
    *,
    include_step_marker: bool = True,
    record_shapes: bool = True,
    num_calls: int = 2,
    graph_launch: bool = False,
    device_name: str = "AMD Instinct MI210",
):
    """Build a minimal, Kineto-shaped raw Chrome trace with one GEMM (aten::addmm).

    Shape: bias(11008), mat1 (32, 4096) x mat2 (4096, 11008), bf16. Each call
    gets its own cpu_op -> hip_runtime launch -> kernel triple, linked by
    "External id" and "correlation" the way roctracer/Kineto traces link them
    (see ``_build_op_to_kernels`` in ``analyze_torch_profile.py``).
    """
    events: list[dict] = [{"ph": "M", "name": "process_name", "pid": 1, "args": {"name": "python"}}]
    if include_step_marker:
        events.append(
            {
                "ph": "X",
                "cat": "user_annotation",
                "name": "ProfilerStep#1",
                "pid": 1,
                "tid": 1,
                "ts": 0,
                "dur": 5000,
                "args": {},
            }
        )
    ts = 100
    for i in range(num_calls):
        args = {
            "Input type": ["c10::BFloat16", "c10::BFloat16", "c10::BFloat16"],
            "External id": 1000 + i,
        }
        if record_shapes:
            args["Input Dims"] = [[11008], [32, 4096], [4096, 11008]]
        events.append(
            {
                "ph": "X",
                "cat": "cpu_op",
                "name": "aten::addmm",
                "pid": 1,
                "tid": 1,
                "ts": ts,
                "dur": 200,
                "args": args,
            }
        )
        events.append(
            {
                "ph": "X",
                "cat": "hip_runtime",
                "name": "hipLaunchKernel",
                "pid": 1,
                "tid": 1,
                "ts": ts + 10,
                "dur": 5,
                "args": {"External id": 1000 + i, "correlation": 5000 + i},
            }
        )
        kernel_name = "hipGraphLaunch" if graph_launch else "Cijk_Ailk_Bljk_HHS_BH_MT128x128"
        events.append(
            {
                "ph": "X",
                "cat": "kernel",
                "name": kernel_name,
                "pid": 0,
                "tid": 2,
                "ts": ts + 20,
                "dur": 300,
                "args": {"correlation": 5000 + i, "device": 0},
            }
        )
        ts += 400
    return {
        "schemaVersion": 1,
        "deviceProperties": [{"id": 0, "name": device_name}],
        "traceEvents": events,
    }


class TestTorchMcpServer:
    def test_registers_expected_tools(self, torch_server_mod: ModuleType) -> None:
        server = torch_server_mod.build_server()
        names = asyncio.run(_list_tool_names(server))
        assert names == {
            "profile_ops",
            "start_target",
            "stop_target",
            "targets",
            "tables",
            "kernels",
            "operators",
            "cpu_overhead",
            "memory",
            "summary",
            "certify",
            "gemm_shapes",
            "roofline",
        }

    def test_tables_tool_reports_prof_json_overview(
        self, torch_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        prof = tmp_path / "prof.json"
        prof.write_text(
            json.dumps(
                {
                    "version": 1,
                    "captured_at": "2026-04-22T00:00:00Z",
                    "mode": "model",
                    "total_cuda_time_us": 1234.5,
                    "total_cpu_time_us": 567.8,
                    "events": [
                        {
                            "name": "aten::mm",
                            "category": "operator",
                            "cpu_time_us": 100,
                            "cuda_time_us": 200,
                            "self_cpu_time_us": 50,
                            "self_cuda_time_us": 200,
                            "count": 3,
                        },
                        {
                            "name": "flash_fwd_kernel",
                            "category": "kernel",
                            "cpu_time_us": 10,
                            "cuda_time_us": 500,
                            "self_cpu_time_us": 10,
                            "self_cuda_time_us": 500,
                            "count": 2,
                        },
                    ],
                }
            )
        )

        server = torch_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "tables", report=str(prof)))
        assert "2026-04-22" in out
        assert "kernel" in out
        assert "operator" in out

    def test_kernels_tool_ranks_by_self_cuda(
        self, torch_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        prof = tmp_path / "prof.json"
        prof.write_text(
            json.dumps(
                {
                    "version": 1,
                    "total_cuda_time_us": 700.0,
                    "total_cpu_time_us": 110.0,
                    "events": [
                        {
                            "name": "flash_fwd_kernel",
                            "category": "kernel",
                            "cpu_time_us": 10,
                            "cuda_time_us": 500,
                            "self_cpu_time_us": 10,
                            "self_cuda_time_us": 500,
                            "count": 2,
                        },
                        {
                            "name": "rms_norm_kernel",
                            "category": "kernel",
                            "cpu_time_us": 5,
                            "cuda_time_us": 200,
                            "self_cpu_time_us": 5,
                            "self_cuda_time_us": 200,
                            "count": 8,
                        },
                    ],
                }
            )
        )

        server = torch_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "kernels", report=str(prof), top=5))
        # flash_fwd_kernel is the bigger one and should appear first.
        assert out.index("flash_fwd_kernel") < out.index("rms_norm_kernel")

    def test_kernels_tool_also_accepts_a_raw_chrome_trace(self, torch_server_mod, tmp_path):  # noqa: ANN001, ANN201  # LW-910184; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this function's return type is intentionally left loose; annotating it now is separate cleanup work
        trace = tmp_path / "trace.pt.trace.json"
        trace.write_text(json.dumps(_chrome_trace_events(num_calls=2)))

        server = torch_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "kernels", report=str(trace)))
        assert "Cijk_Ailk_Bljk_HHS_BH_MT128x128" in out

    def test_certify_passes_a_well_formed_trace(self, torch_server_mod, tmp_path):  # noqa: ANN001, ANN201  # LW-910185; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this function's return type is intentionally left loose; annotating it now is separate cleanup work
        trace = tmp_path / "trace.pt.trace.json"
        trace.write_text(json.dumps(_chrome_trace_events(num_calls=1)))

        server = torch_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "certify", trace=str(trace)))
        assert "record_shapes" in out
        assert "step_markers" in out
        assert "[PASS]" in out

    def test_certify_fails_without_record_shapes(self, torch_server_mod, tmp_path):  # noqa: ANN001, ANN201  # LW-910186; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this function's return type is intentionally left loose; annotating it now is separate cleanup work
        trace = tmp_path / "trace.pt.trace.json"
        trace.write_text(json.dumps(_chrome_trace_events(record_shapes=False, num_calls=1)))

        server = torch_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "certify", trace=str(trace)))
        assert "Trace certification: FAIL" in out
        assert "record_shapes=True" in out

    def test_certify_flags_graph_replay_as_degraded_attribution(self, torch_server_mod, tmp_path):  # noqa: ANN001, ANN201  # LW-910187; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this function's return type is intentionally left loose; annotating it now is separate cleanup work
        trace = tmp_path / "trace.pt.trace.json"
        trace.write_text(json.dumps(_chrome_trace_events(graph_launch=True, num_calls=1)))

        server = torch_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "certify", trace=str(trace)))
        assert "graph_replay" in out
        assert "hipGraphLaunch" in out

    def test_certify_rejects_a_summarized_report(self, torch_server_mod, tmp_path):  # noqa: ANN001, ANN201  # LW-910188; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this function's return type is intentionally left loose; annotating it now is separate cleanup work
        prof = tmp_path / "prof.json"
        prof.write_text(
            json.dumps(
                {"version": 1, "total_cuda_time_us": 1.0, "total_cpu_time_us": 1.0, "events": []}
            )
        )

        server = torch_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "certify", trace=str(prof)))
        assert out.startswith("error:")
        assert "not a raw Kineto/Chrome trace" in out

    def test_gemm_shapes_dedups_and_ranks_by_gpu_time(self, torch_server_mod, tmp_path):  # noqa: ANN001, ANN201  # LW-910189; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this function's return type is intentionally left loose; annotating it now is separate cleanup work
        trace = tmp_path / "trace.pt.trace.json"
        trace.write_text(json.dumps(_chrome_trace_events(num_calls=3)))

        server = torch_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "gemm_shapes", trace=str(trace), top=5))
        assert "32x11008x4096" in out
        # Three identical-shape calls collapse into one deduplicated row.
        assert out.count("addmm") == 1
        assert out.count("32x11008x4096") == 1

    def test_gemm_shapes_writes_ranked_json_when_out_given(self, torch_server_mod, tmp_path):  # noqa: ANN001, ANN201  # LW-910190; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this function's return type is intentionally left loose; annotating it now is separate cleanup work
        trace = tmp_path / "trace.pt.trace.json"
        trace.write_text(json.dumps(_chrome_trace_events(num_calls=2)))
        out_path = tmp_path / "shapes.json"

        server = torch_server_mod.build_server()
        asyncio.run(_call_tool(server, "gemm_shapes", trace=str(trace), out=str(out_path)))

        written = json.loads(out_path.read_text())
        assert written[0]["op"] == "addmm"
        assert written[0]["m"] == 32
        assert written[0]["n"] == 11008
        assert written[0]["k"] == 4096
        assert written[0]["call_count"] == 2

    def test_roofline_classifies_bound_and_reports_device_peaks(self, torch_server_mod, tmp_path):  # noqa: ANN001, ANN201  # LW-910191; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this function's return type is intentionally left loose; annotating it now is separate cleanup work
        trace = tmp_path / "trace.pt.trace.json"
        trace.write_text(json.dumps(_chrome_trace_events(num_calls=1)))

        server = torch_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "roofline", trace=str(trace)))
        assert "MI210" in out
        # A skinny M=32 GEMM at this shape is memory-bound on MI210.
        assert "memory" in out

    def test_roofline_prefers_explicit_peaks_over_autodetect(self, torch_server_mod, tmp_path):  # noqa: ANN001, ANN201  # LW-910192; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this function's return type is intentionally left loose; annotating it now is separate cleanup work
        trace = tmp_path / "trace.pt.trace.json"
        trace.write_text(
            json.dumps(_chrome_trace_events(num_calls=1, device_name="Unrecognized GPU"))
        )

        server = torch_server_mod.build_server()
        out = asyncio.run(
            _call_tool(server, "roofline", trace=str(trace), peak_tflops=100.0, peak_gbps=1000.0)
        )
        assert "100.0 TFLOP/s" in out
        assert "1000 GB/s" in out


# ---------------------------------------------------------------------------
# Headroom MCP server
# ---------------------------------------------------------------------------


def _headroom_report(observed_copy: float = 10.9) -> dict:
    return {
        "schema_version": 1,
        "gpu_spec_matched": "B200-SXM-180GB",
        "meta": {"walk": "image_gen"},
        "buckets_ms_per_step": {
            "observed": 21.5,
            "speed_of_light": 9.9,
            "estimated_floor": 4.7,
        },
        "definitions": {"observed": "measured device time per step"},
        "kernels": [
            {
                "kernel": "big_copy_kernel",
                "kind": "eager",
                "class": "movement",
                "observed_ms_step": observed_copy,
                "sol_ms_step": 2.4,
                "opportunity_ms_step": observed_copy,
                "calls_per_step": 144,
                "source": [{"loc": "utils.py:273", "code": "set_kv_cache(...)"}],
            },
            {
                "kernel": "nvjet_gemm",
                "kind": "gemm",
                "class": "quality",
                "observed_ms_step": 1.9,
                "sol_ms_step": 0.8,
                "opportunity_ms_step": 1.1,
            },
        ],
        "caveats": ["bytes assume bf16 elements"],
    }


class TestHeadroomMcpServer:
    def test_registers_expected_tools(self, headroom_server_mod: ModuleType) -> None:
        server = headroom_server_mod.build_server()
        names = asyncio.run(_list_tool_names(server))
        assert names == {
            "waterfall",
            "top",
            "kernel",
            "subgraphs",
            "compare",
            "summary",
        }

    def test_waterfall_reports_buckets_and_definitions(
        self, headroom_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        report = tmp_path / "report.json"
        report.write_text(json.dumps(_headroom_report()))

        server = headroom_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "waterfall", report=str(report)))
        assert "observed" in out
        assert "estimated_floor" in out
        assert "measured device time per step" in out

    def test_top_ranks_by_opportunity_and_filters_by_class(
        self, headroom_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        report = tmp_path / "report.json"
        report.write_text(json.dumps(_headroom_report()))

        server = headroom_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "top", report=str(report)))
        # The copy has the larger opportunity and must rank above the GEMM.
        assert out.index("big_copy_kernel") < out.index("nvjet_gemm")
        assert "utils.py:273" in out

        only_quality = asyncio.run(
            _call_tool(server, "top", report=str(report), klass="quality"),
        )
        assert "nvjet_gemm" in only_quality
        assert "big_copy_kernel" not in only_quality

    def test_kernel_tool_matches_substring(
        self, headroom_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        report = tmp_path / "report.json"
        report.write_text(json.dumps(_headroom_report()))

        server = headroom_server_mod.build_server()
        # The tool's own argument is called ``name``, which collides with the
        # ``_call_tool`` helper's positional; call the server directly.
        _, structured = asyncio.run(
            server.call_tool("kernel", {"report": str(report), "name": "nvjet"})
        )
        out = structured["result"]
        assert "nvjet_gemm" in out
        assert "big_copy_kernel" not in out

    def test_compare_reports_per_kernel_delta(
        self, headroom_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        old = tmp_path / "old.json"
        new = tmp_path / "new.json"
        old.write_text(json.dumps(_headroom_report(observed_copy=10.9)))
        new.write_text(json.dumps(_headroom_report(observed_copy=2.0)))

        server = headroom_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "compare", old=str(old), new=str(new)))
        assert "big_copy_kernel" in out
        assert "-8.900" in out

    def test_malformed_report_is_a_structured_error(
        self, headroom_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        bogus = tmp_path / "bogus.json"
        bogus.write_text(json.dumps({"not_kernels": []}))

        server = headroom_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "summary", report=str(bogus)))
        assert out.startswith("error:")
        assert "not a headroom report" in out


# ---------------------------------------------------------------------------
# rocprof MCP server
# ---------------------------------------------------------------------------

_ROCPROF_FIXTURES = Path(__file__).parent / "fixtures" / "rocprof"


def _rocprof_kernel_trace_dir(tmp_path: Path) -> Path:
    """A minimal single-process rocprofv3 kernel-trace directory."""
    d = tmp_path / "gpu-node-01" / "4242"
    d.mkdir(parents=True)
    (d / "out_kernel_trace.csv").write_text(
        "Kernel_Name,Agent_Id,Queue_Id,Correlation_Id,Start_Timestamp,End_Timestamp,Pid\n"
        "flash_attn_decode_kernel,0,0,1,1000,2000,4242\n"
    )
    return tmp_path


# A minimal fake rocprofv3 for the MCP-layer profile_timeline smoke test below
# (the deep behavior coverage for capture.py's profile_* tools lives in
# tests/vibesys/loops/test_rocprof_capture.py -- this only proves server.py's
# wiring reaches capture.py correctly). No SIGINT handling is needed here:
# the smoke test uses a trivial bounded command with no load_command.
_FAKE_ROCPROFV3_TIMELINE_SOURCE = textwrap.dedent(
    f"""
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    argv = sys.argv[1:]
    out_dir = None
    for i, tok in enumerate(argv):
        if tok == "-d" and i + 1 < len(argv):
            out_dir = argv[i + 1]
            break
    wrapped = argv[argv.index("--") + 1 :] if "--" in argv else []
    rc = subprocess.Popen(wrapped).wait() if wrapped else 0
    if rc == 0 and out_dir:
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        src = Path({str(_ROCPROF_FIXTURES)!r}) / "kernel_trace" / "out_kernel_trace.csv"
        if src.is_file():
            shutil.copy(src, out_path / "out_kernel_trace.csv")
    sys.exit(rc)
    """
)


def _install_fake_rocprofv3_timeline_only(bin_dir: Path) -> Path:
    path = bin_dir / "rocprofv3"
    path.write_text(f"#!{sys.executable}\n{_FAKE_ROCPROFV3_TIMELINE_SOURCE}")
    path.chmod(0o755)
    return path


# -- MCP-level responsiveness / cancellation / busy-guard smoke tests --------
#
# These exercise the async wiring (server.py's `async def profile_timeline`
# + mcp_async.run_cancellable) through FastMCP's own in-memory
# `call_tool`/`list_tools` -- the "in-memory client/server" the pinned
# mcp<2 SDK provides, no stdio subprocess needed (see mcp_async.py's
# docstring). The deep capture_runtime-level coverage for cancel_event/
# exclusive_capture lives in test_capture_runtime.py; this proves the MCP
# tool wrapper actually reaches that machinery, not just capture_runtime in
# isolation.

_SLOW_TARGET_SOURCE = textwrap.dedent(
    """
    import os
    import signal
    import sys

    # Keep the fifo's write end open for the process's whole life: the test
    # observes the target's death as EOF on the read end.
    handle = open(sys.argv[1], "w")
    handle.write(str(os.getpid()))
    handle.flush()
    signal.pause()
    """
)


def _install_slow_target(tmp_path: Path) -> Path:
    path = tmp_path / "slow_target.py"
    path.write_text(_SLOW_TARGET_SOURCE)
    return path


class _PidChannel:
    """A FIFO the slow target reports its pid on, awaited without polling.

    The read end is opened (non-blocking) before the target starts, so the
    target's ``open(fifo, "w")`` succeeds immediately, and the event loop is
    woken by the fd becoming readable rather than by a timer.
    """

    def __init__(self, path: Path) -> None:
        os.mkfifo(path)
        self.path = path
        self._fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)

    async def wait_for_start(self, capture_task: asyncio.Future[str]) -> None:
        """Return once the target has reported in, or fail if the capture ends first."""
        loop = asyncio.get_running_loop()
        readable = asyncio.Event()
        loop.add_reader(self._fd, readable.set)
        waiter = asyncio.ensure_future(readable.wait())
        try:
            await asyncio.wait({waiter, capture_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            loop.remove_reader(self._fd)
            waiter.cancel()
        if not readable.is_set():
            diag = f"capture_task.done()={capture_task.done()}"
            if capture_task.done():
                exc = capture_task.exception()
                diag += f" exception={exc!r}"
                if exc is None:
                    diag += f" result={capture_task.result()!r}"
            raise AssertionError(f"target never started: {diag}")  # noqa: TRY003  # LW-910203; this is a boundary error that deliberately embeds the offending value for the operator to act on
        os.read(self._fd, 64)

    def wait_for_target_exit(self, *, timeout_s: float = 30.0) -> bool:
        """True once the target has exited: its held-open write end closes, giving EOF.

        ``timeout_s`` only bounds the failure case where the process leaked.
        """
        readable, _, _ = select.select([self._fd], [], [], timeout_s)
        return bool(readable) and os.read(self._fd, 64) == b""

    def close(self) -> None:
        os.close(self._fd)


class _WorkerTracker:
    """``run_worker`` seam Fake: runs the real ``run_cancellable``, tracking in-flight workers.

    A cancelled call's worker thread is "abandoned" (anyio's
    ``abandon_on_cancel=True``): it keeps escalating the target and releasing
    the exclusive-capture slot in its own ``finally`` after the coroutine that
    awaited cancellation has already returned. ``wait_idle`` blocks on a
    condition until every such worker has returned, so it cannot race a
    subsequent capture.
    """

    def __init__(self, run_cancellable: Callable[..., Awaitable[str]]) -> None:
        self._run_cancellable = run_cancellable
        self._in_flight = 0
        self._idle = threading.Condition()

    async def __call__(
        self,
        fn: Callable[..., str],
        /,
        *args: object,
        cancel_event: threading.Event,
        **kwargs: object,
    ) -> str:
        with self._idle:
            self._in_flight += 1

        def tracked(*call_args: object, **call_kwargs: object) -> str:
            try:
                return fn(*call_args, **call_kwargs)
            finally:
                with self._idle:
                    self._in_flight -= 1
                    self._idle.notify_all()

        return await self._run_cancellable(tracked, *args, cancel_event=cancel_event, **kwargs)

    def wait_idle(self, *, timeout_s: float = 30.0) -> bool:
        """``timeout_s`` only bounds the failure case where a worker never returns."""
        with self._idle:
            return self._idle.wait_for(lambda: self._in_flight == 0, timeout=timeout_s)


_FAKE_PANDAS_PYTHON_SOURCE = textwrap.dedent(
    """\
    #!/bin/sh
    if [ "$1" = "-c" ]; then
        echo 2.2.2
    else
        echo "rocprofiler-compute version: 3.1.0"
    fi
    """
)


def _install_fake_rocm(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A minimal ROCm tree plus a stand-in deps interpreter, selected via env vars."""
    bin_dir = root / "bin"
    lib_dir = root / "lib"
    bin_dir.mkdir(parents=True)
    lib_dir.mkdir()
    for name in ("rocprof-compute", "rocprof"):
        tool = bin_dir / name
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
    (lib_dir / "libaqlprofile64.so").write_bytes(b"")
    fake_python = root / "fake-python"
    fake_python.write_text(_FAKE_PANDAS_PYTHON_SOURCE)
    fake_python.chmod(0o755)
    monkeypatch.setenv("ROCM_PATH", str(root))
    monkeypatch.setenv("VIBESYS_ROCPROF_COMPUTE_BIN", str(bin_dir / "rocprof-compute"))
    monkeypatch.setenv("VIBESYS_ROCPROF_COMPUTE_PYTHON", str(fake_python))


class TestRocprofMcpServer:
    @pytest.fixture(autouse=True)
    def _reset_capture_slot(self, rocprof_server_mod: ModuleType) -> Iterator[None]:
        """Every test starts and ends with no capture holding the in-process slot.

        ``capture_runtime``'s exclusive-capture slot is process-global
        module state (see ``test_capture_runtime.py``'s own reset fixture):
        a cancelled capture's worker thread releases it only once its
        (possibly still-escalating) teardown finishes, which can outlast
        the coroutine that awaited its cancellation. Without this reset, a
        cancellation test earlier in this class can leave the slot held
        into the next test and make an unrelated capture spuriously report
        "busy".
        """
        rocprof_server_mod.capture_runtime.release_capture_slot()
        yield
        rocprof_server_mod.capture_runtime.release_capture_slot()

    def test_registers_expected_tools(self, rocprof_server_mod):  # noqa: ANN001, ANN201  # LW-910195; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this function's return type is intentionally left loose; annotating it now is separate cleanup work
        # Built from the live server rather than assumed: counter_sets/
        # counter_plan/att_plan were folded into profiling_capabilities, and
        # compute_doctor was folded into it too (reached via
        # capture.profiling_capabilities() -> _capability_compute_block()).
        # certify/gemm_shapes/roofline are only registered when the torch
        # analyzer module is importable as a sibling; since
        # resources/profilers/torch/analyze_torch_profile.py exists on disk
        # in this repo, they ARE registered in this test environment.
        server = rocprof_server_mod.build_server()
        names = asyncio.run(_list_tool_names(server))
        assert names == {
            # capabilities + capture tools (capture.py)
            "profiling_capabilities",
            "profile_timeline",
            "profile_counters",
            "profile_kernel_deep",
            "profile_instructions",
            "profile_ops",
            "start_target",
            "stop_target",
            "targets",
            # capture store: list / summarize / diff
            "captures",
            "summary",
            "compare",
            # analyze_rocprof.py: rocprofv3 system trace
            "files",
            "kernels",
            "families",
            "idle_gaps",
            "cpu_overhead",
            "memory",
            "graphs",
            "host_idle",
            "query",
            # counters.py: PMC counter sets
            "counter_report",
            "counter_triage",
            # att.py: Advanced Thread Trace
            "att_hotspots",
            # compute.py: rocprof-compute analyze drill-down
            "compute_analyze",
            # analyze_torch_profile.py: cross-check torch-side analysis
            "certify",
            "gemm_shapes",
            "roofline",
            # kernel_bench.py: microbenchmark + paired A/B
            "bench_parse",
            "bench_compare",
            "bench_verdict",
        }

    def test_registers_fewer_tools_when_torch_analyzer_is_unavailable(
        self, rocprof_server_mod: ModuleType
    ) -> None:
        server = rocprof_server_mod.build_server(import_sibling=lambda _name: None)
        names = asyncio.run(_list_tool_names(server))

        assert "certify" not in names
        assert "gemm_shapes" not in names
        assert "roofline" not in names
        assert "profile_timeline" in names

    def test_kernels_tool_reports_from_a_kernel_trace_directory(
        self, rocprof_server_mod: ModuleType, tmp_path: Path
    ) -> None:
        report = _rocprof_kernel_trace_dir(tmp_path)

        server = rocprof_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "kernels", report=str(report), top=5))
        assert "flash_attn_decode_kernel" in out

    def test_counter_report_tool_merges_pmc_passes(self, rocprof_server_mod):  # noqa: ANN001, ANN201  # LW-910196; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this function's return type is intentionally left loose; annotating it now is separate cleanup work
        dirs = [
            str(_ROCPROF_FIXTURES / "pmc" / "l2"),
            str(_ROCPROF_FIXTURES / "pmc" / "hbm"),
            str(_ROCPROF_FIXTURES / "kernel_trace"),
        ]

        server = rocprof_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "counter_report", dirs=dirs, top=15, arch="gfx90a"))
        assert "flash_attn_decode_kernel" in out
        assert "L2 hit rate:" in out

    def test_counter_triage_tool_classifies_bottlenecks(self, rocprof_server_mod):  # noqa: ANN001, ANN201  # LW-910197; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this function's return type is intentionally left loose; annotating it now is separate cleanup work
        dirs = [str(_ROCPROF_FIXTURES / "pmc" / "occupancy_low")]

        server = rocprof_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "counter_triage", dirs=dirs, arch="gfx90a", top=15))
        assert "gemv_lowocc_kernel" in out
        assert "OCCUPANCY-LIMITED" in out

    def test_att_hotspots_tool_ranks_stall_hotspots(self, rocprof_server_mod):  # noqa: ANN001, ANN201  # LW-910198; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this function's return type is intentionally left loose; annotating it now is separate cleanup work
        dispatch_dir = _ROCPROF_FIXTURES / "att" / "ui_output_agent_123_dispatch_1"

        server = rocprof_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "att_hotspots", dispatch_dir=str(dispatch_dir), top=5))
        assert "Stall category totals" in out

    def test_profiling_capabilities_tool_reports_all_compute_checks_passed(
        self, rocprof_server_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # compute_doctor is gone as a standalone tool; its diagnosis is now
        # reached via profiling_capabilities() -> capture._capability_compute_block()
        # -> compute.cmd_doctor, which locates everything through env vars.
        _install_fake_rocm(tmp_path / "rocm", monkeypatch)

        server = rocprof_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "profiling_capabilities"))
        assert "[OK]" in out
        assert "All checks passed." in out

    def test_profiling_capabilities_tool_reports_missing_compute_binary_as_a_fix(
        self, rocprof_server_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VIBESYS_ROCPROF_COMPUTE_BIN", str(tmp_path / "no-such-rocprof-compute"))

        server = rocprof_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "profiling_capabilities"))
        assert "[FAIL] rocprof-compute binary: not found" in out
        assert "Fixes:" in out

    def test_compute_analyze_tool_falls_back_to_raw_csvs(
        self, rocprof_server_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VIBESYS_ROCPROF_COMPUTE_BIN", str(tmp_path / "no-such-rocprof-compute"))
        workload = tmp_path / "workload"
        workload.mkdir()
        (workload / "pmc_perf.csv").write_text("a,b\n1,2\n")

        server = rocprof_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "compute_analyze", workload_dir=str(workload)))
        assert "falling back to raw CSVs" in out
        assert "pmc_perf.csv" in out

    def test_bench_parse_tool_extracts_wall_ms_lines(self, rocprof_server_mod, tmp_path):  # noqa: ANN001, ANN201  # LW-910199; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this function's return type is intentionally left loose; annotating it now is separate cleanup work
        log = tmp_path / "driver.log"
        log.write_text("wall_ms: 12.5\nwall_ms: 13.0\n")

        server = rocprof_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "bench_parse", log=str(log)))
        assert json.loads(out) == {"wall_ms": [12.5, 13.0]}

    def test_bench_compare_tool_reports_a_decisive_verdict(self, rocprof_server_mod, tmp_path):  # noqa: ANN001, ANN201  # LW-910200; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this function's return type is intentionally left loose; annotating it now is separate cleanup work
        file_a = tmp_path / "a.json"
        file_b = tmp_path / "b.json"
        file_a.write_text(json.dumps({"wall_ms": [10.0, 10.1, 9.9, 10.0]}))
        file_b.write_text(json.dumps({"wall_ms": [8.0, 8.1, 7.9, 8.0]}))

        server = rocprof_server_mod.build_server()
        out = asyncio.run(
            _call_tool(server, "bench_compare", file_a=str(file_a), file_b=str(file_b))
        )
        assert "DECISIVE" in out

    def test_bench_verdict_tool_reports_a_paired_verdict(self, rocprof_server_mod, tmp_path):  # noqa: ANN001, ANN201  # LW-910201; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this function's return type is intentionally left loose; annotating it now is separate cleanup work
        samples = tmp_path / "pairs.json"
        samples.write_text(
            json.dumps(
                {
                    "pairs": [
                        [10.0, 8.0],
                        [10.0, 8.1],
                        [10.0, 7.9],
                        [10.0, 8.0],
                    ]
                }
            )
        )

        server = rocprof_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "bench_verdict", samples=str(samples)))
        assert "DECISIVE" in out

    # -- new curated-surface smoke tests: prove server.py's wiring, not the
    # -- deep capture.py behavior (that's tests/vibesys/loops/test_rocprof_capture.py) --

    def test_profiling_capabilities_tool_smoke(self, rocprof_server_mod):  # noqa: ANN001, ANN201  # LW-910202; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this function's return type is intentionally left loose; annotating it now is separate cleanup work
        server = rocprof_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "profiling_capabilities"))
        assert out
        assert "rocprofv3" in out

    def test_profile_timeline_tool_with_fake_rocprofv3_returns_ok(
        self, rocprof_server_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir()
        _install_fake_rocprofv3_timeline_only(bin_dir)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(tmp_path / ".profiles"))

        server = rocprof_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "profile_timeline", command="true"))

        assert "): ok" in out

    def test_captures_tool_with_no_captures_reports_the_empty_message(
        self, rocprof_server_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(tmp_path / ".profiles"))
        server = rocprof_server_mod.build_server()

        out = asyncio.run(_call_tool(server, "captures"))

        assert "(no captures" in out

    def test_summary_and_compare_tools_with_a_bogus_capture_id_return_clean_error_text(
        self, rocprof_server_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(tmp_path / ".profiles"))
        server = rocprof_server_mod.build_server()

        summary_out = asyncio.run(_call_tool(server, "summary", capture_id="bogus-capture-id"))
        compare_out = asyncio.run(_call_tool(server, "compare", a="bogus-a", b="bogus-b"))

        # This module's own clean "error: ..." text, not a raised exception
        # turned into FastMCP's own error shape or a stack trace.
        assert summary_out.startswith("error:")
        assert compare_out.startswith("error:")

    def test_profile_counters_tool_with_an_unknown_set_returns_error_string(
        self, rocprof_server_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # capture.profile_counters *raises* ValueError for bad input; the
        # profile_counters tool wrapper in server.py catches ValueError and
        # returns f"error: {exc}" instead of letting it propagate.
        monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(tmp_path / ".profiles"))
        server = rocprof_server_mod.build_server()

        out = asyncio.run(
            _call_tool(server, "profile_counters", command="true", sets=["bogus-set-xyz"])
        )

        assert out.startswith("error:")

    def _slow_capture_server(
        self, rocprof_server_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[_McpServer, _WorkerTracker, _PidChannel, str]:
        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir()
        _install_fake_rocprofv3_timeline_only(bin_dir)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(tmp_path / ".profiles"))
        pid_channel = _PidChannel(tmp_path / "target.pid")
        slow_script = _install_slow_target(tmp_path)
        tracker = _WorkerTracker(rocprof_server_mod.mcp_async.run_cancellable)
        server = rocprof_server_mod.build_server(run_worker=tracker)
        return server, tracker, pid_channel, f"{sys.executable} {slow_script} {pid_channel.path}"

    def _assert_target_and_slot_released(
        self, rocprof_server_mod: ModuleType, tracker: _WorkerTracker, pid_channel: _PidChannel
    ) -> None:
        assert pid_channel.wait_for_target_exit(), "target process leaked past cancellation"
        # The cancelled call's worker thread keeps running (escalating, then
        # releasing the exclusive-capture slot in its own `finally`) after the
        # coroutine that awaited cancellation has already returned. Wait for
        # that teardown to finish so it can't race a subsequent test's own
        # capture.
        assert tracker.wait_idle(), "capture worker never returned after cancellation"
        assert rocprof_server_mod.capture_runtime.active_capture() is None, (
            "capture slot still held after cancellation settled"
        )

    def test_profile_timeline_tool_stays_responsive_to_other_calls_while_a_capture_runs(
        self, rocprof_server_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server, tracker, pid_channel, command = self._slow_capture_server(
            rocprof_server_mod, tmp_path, monkeypatch
        )

        async def run() -> None:
            capture_task = asyncio.ensure_future(
                _call_tool(server, "profile_timeline", command=command)
            )
            await pid_channel.wait_for_start(capture_task)

            # A cheap call, made while the capture is still in flight, must
            # return promptly instead of waiting behind it on the event
            # loop -- this is the whole point of running the capture off
            # the main loop.
            cheap_out = await asyncio.wait_for(_call_tool(server, "captures"), timeout=3.0)
            assert cheap_out
            assert not capture_task.done()

            capture_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await capture_task

        try:
            asyncio.run(run())
            self._assert_target_and_slot_released(rocprof_server_mod, tracker, pid_channel)
        finally:
            pid_channel.close()

    def test_profile_timeline_tool_returns_busy_for_a_second_concurrent_capture(
        self, rocprof_server_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server, tracker, pid_channel, command = self._slow_capture_server(
            rocprof_server_mod, tmp_path, monkeypatch
        )

        async def run() -> str:
            capture_task = asyncio.ensure_future(
                _call_tool(server, "profile_timeline", command=command)
            )
            await pid_channel.wait_for_start(capture_task)

            busy_out = await asyncio.wait_for(
                _call_tool(server, "profile_timeline", command="true"), timeout=3.0
            )

            capture_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await capture_task
            return busy_out

        try:
            busy_out = asyncio.run(run())
            assert busy_out.startswith("busy: capture ")
            assert "timeline" in busy_out
            self._assert_target_and_slot_released(rocprof_server_mod, tracker, pid_channel)
        finally:
            pid_channel.close()

    def test_profile_timeline_tool_cancellation_stops_the_target_process(
        self, rocprof_server_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server, tracker, pid_channel, command = self._slow_capture_server(
            rocprof_server_mod, tmp_path, monkeypatch
        )

        async def run() -> None:
            task = asyncio.ensure_future(_call_tool(server, "profile_timeline", command=command))
            await pid_channel.wait_for_start(task)

            # Simulates a client-initiated cancellation (e.g. an MCP client
            # tearing down after its own timeout): cancelling the awaiting
            # task must not just stop watching -- it must stop the capture.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(task, timeout=0.5)

        start = time.monotonic()
        try:
            asyncio.run(run())
            elapsed = time.monotonic() - start

            # Bounded by capture_runtime's poll chunk + escalation, not by the
            # target's own (never reached) indefinite wait.
            assert elapsed < 10.0
            self._assert_target_and_slot_released(rocprof_server_mod, tracker, pid_channel)
        finally:
            pid_channel.close()
