"""Tests for the profiler MCP servers.

We verify tool registration via ``FastMCP.list_tools`` and exercise a few
tools end-to-end through ``FastMCP.call_tool``. The stdio JSON-RPC framing
itself is the ``mcp`` package's responsibility.
"""

import asyncio
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest

from vibesys.loops.profiler import mcp_spec
from vibesys.profilers import ProfilerKind


# The servers live under resources/ (co-located with the analysis scripts) so
# importing them by file path keeps the tests decoupled from sys.path state.
def _load_module(name: str, path: Path):  # noqa: ANN202  # tracked: #288
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


def test_profiler_mcp_spec_maps_known_kinds_exactly():  # noqa: ANN201  # tracked: #288
    assert mcp_spec(ProfilerKind.NONE) is None

    nsys = mcp_spec(ProfilerKind.NSYS)
    assert nsys is not None
    assert nsys.name == "vibesys-nsys-profiler"
    assert nsys.args == ("nsys_profiler/server.py",)

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


def test_profiler_mcp_spec_rejects_unknown_kind():  # noqa: ANN201  # tracked: #288
    # The rejection is a runtime guard against a value the annotation forbids,
    # so route the bad argument through an untyped mapping.
    invalid_kwargs: dict = {"profiler_kind": "bogus"}
    with pytest.raises(TypeError, match="ProfilerKind"):
        mcp_spec(**invalid_kwargs)


@pytest.fixture(scope="module")
def nsys_server_mod():  # noqa: ANN201  # tracked: #288
    return _load_module(
        "_nsys_server",
        _REPO / "resources" / "profilers" / "nsys" / "server.py",
    )


@pytest.fixture(scope="module")
def torch_server_mod():  # noqa: ANN201  # tracked: #288
    return _load_module(
        "_torch_server",
        _REPO / "resources" / "profilers" / "torch" / "server.py",
    )


@pytest.fixture(scope="module")
def otel_server_mod():  # noqa: ANN201  # tracked: #288
    return _load_module(
        "_otel_server",
        _REPO / "resources" / "profilers" / "otel" / "server.py",
    )


@pytest.fixture(scope="module")
def headroom_server_mod():  # noqa: ANN201  # tracked: #288
    return _load_module(
        "_headroom_server",
        _REPO / "resources" / "profilers" / "headroom" / "server.py",
    )


async def _list_tool_names(server) -> set[str]:  # noqa: ANN001  # tracked: #288
    tools = await server.list_tools()
    return {t.name for t in tools}


async def _call_tool(server, name: str, **kwargs) -> str:  # noqa: ANN001, ANN003  # tracked: #288
    _, structured = await server.call_tool(name, kwargs)
    return structured["result"]


async def _call_structured_tool(server, name: str, **kwargs) -> dict:  # noqa: ANN001, ANN003  # tracked: #288
    _, structured = await server.call_tool(name, kwargs)
    return structured


# ---------------------------------------------------------------------------
# nsys MCP server
# ---------------------------------------------------------------------------


class TestNsysMcpServer:
    def test_registers_expected_tools(self, nsys_server_mod):  # noqa: ANN001, ANN201  # tracked: #288
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

    def test_tables_tool_reports_empty_db(self, nsys_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        """Against an empty SQLite file, ``tables`` returns a no-output marker."""
        db = tmp_path / "empty.sqlite"
        sqlite3.connect(str(db)).close()

        server = nsys_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "tables", report=str(db)))
        # No tables → the cmd_tables body prints nothing; the wrapper
        # coerces that to "(no output)".
        assert out == "(no output)"

    def test_kernels_tool_reports_no_data(self, nsys_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        """A SQLite file without a CUPTI_ACTIVITY_KIND_KERNEL table returns a friendly message."""
        db = tmp_path / "nokernels.sqlite"
        sqlite3.connect(str(db)).close()

        server = nsys_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "kernels", report=str(db)))
        assert "No kernel data" in out

    def test_query_tool_runs_arbitrary_sql(self, nsys_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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
    def test_registers_expected_tools(self, otel_server_mod):  # noqa: ANN001, ANN201  # tracked: #288
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

    def test_discovers_and_summarizes_critical_path(self, otel_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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

    def test_critical_path_tool_returns_structured_summary(self, otel_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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
        assert result["roots"][0]["nodes_by_contribution"][0]["service"] == "search"

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
        assert result["roots"][0]["representative_trace"]["trace_id"] == "trace-a"
        assert result["roots"][0]["nodes"][1]["service"] == "search"

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

    def test_summary_and_compare_use_normalized_service_rows(self, otel_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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

    def test_accepts_report_produced_by_go_otelcapture(self, otel_server_mod):  # noqa: ANN001, ANN201  # tracked: #288
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
    def test_compare_rejects_incompatible_reports(self, otel_server_mod, tmp_path, identity_field):  # noqa: ANN001, ANN201  # tracked: #288
        before = tmp_path / "before.json"
        after = tmp_path / "after.json"
        before.write_text(json.dumps(_otel_report(20.0)))
        after_report = _otel_report(12.0)
        after_report[identity_field] = "different"
        after.write_text(json.dumps(after_report))

        with pytest.raises(ValueError, match="matching workload identity"):
            otel_server_mod.compare_reports(str(before), str(after))

    def test_compare_allows_run_specific_measurement_timestamps(self, otel_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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

    def test_load_report_rejects_invalid_aggregate_error_count(self, otel_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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
    def test_load_report_rejects_malformed_contract(self, otel_server_mod, tmp_path, mutate):  # noqa: ANN001, ANN201  # tracked: #288
        report = _otel_report(20.0)
        mutate(report)
        path = tmp_path / "invalid.json"
        path.write_text(json.dumps(report))

        with pytest.raises(ValueError):  # noqa: PT011  # tracked: #288
            otel_server_mod.load_report(str(path))

    def test_summary_rejects_non_positive_top(self, otel_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        path = tmp_path / "report.json"
        path.write_text(json.dumps(_otel_report(20.0)))

        with pytest.raises(ValueError, match="top must be positive"):
            otel_server_mod.summarize_report(str(path), top=0)

    def test_compare_rejects_non_positive_top_before_reading_files(self, otel_server_mod):  # noqa: ANN001, ANN201  # tracked: #288
        # top is validated before any file I/O, so unreadable paths do not matter.
        with pytest.raises(ValueError, match="top must be positive"):
            otel_server_mod.compare_reports("missing-before.json", "missing-after.json", top=0)

    def test_find_reports_skips_hostile_json(self, otel_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        valid = tmp_path / "valid.json"
        valid.write_text(json.dumps(_otel_report(20.0)))
        # A candidate under evaluation controls workspace files; none of these
        # may abort discovery of the valid report.
        (tmp_path / "binary.json").write_bytes(b"\xff\xfe\x00\x01 not valid utf-8")
        (tmp_path / "nested.json").write_text("[" * 3000 + "]" * 3000)
        (tmp_path / "bigint.json").write_text('{"schema_version": ' + "9" * 5000 + "}")
        (tmp_path / "truncated.json").write_text('{"schema_version": 1')

        assert otel_server_mod.find_reports(str(tmp_path)) == [valid.as_posix()]

    def test_compare_surfaces_rows_present_in_one_report(self, otel_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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


class TestTorchMcpServer:
    def test_registers_expected_tools(self, torch_server_mod):  # noqa: ANN001, ANN201  # tracked: #288
        server = torch_server_mod.build_server()
        names = asyncio.run(_list_tool_names(server))
        assert names == {
            "tables",
            "kernels",
            "operators",
            "cpu_overhead",
            "memory",
            "summary",
        }

    def test_tables_tool_reports_prof_json_overview(self, torch_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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

    def test_kernels_tool_ranks_by_self_cuda(self, torch_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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
    def test_registers_expected_tools(self, headroom_server_mod):  # noqa: ANN001, ANN201  # tracked: #288
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

    def test_waterfall_reports_buckets_and_definitions(self, headroom_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        report = tmp_path / "report.json"
        report.write_text(json.dumps(_headroom_report()))

        server = headroom_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "waterfall", report=str(report)))
        assert "observed" in out
        assert "estimated_floor" in out
        assert "measured device time per step" in out

    def test_top_ranks_by_opportunity_and_filters_by_class(self, headroom_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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

    def test_kernel_tool_matches_substring(self, headroom_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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

    def test_compare_reports_per_kernel_delta(self, headroom_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        old = tmp_path / "old.json"
        new = tmp_path / "new.json"
        old.write_text(json.dumps(_headroom_report(observed_copy=10.9)))
        new.write_text(json.dumps(_headroom_report(observed_copy=2.0)))

        server = headroom_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "compare", old=str(old), new=str(new)))
        assert "big_copy_kernel" in out
        assert "-8.900" in out

    def test_malformed_report_is_a_structured_error(self, headroom_server_mod, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        bogus = tmp_path / "bogus.json"
        bogus.write_text(json.dumps({"not_kernels": []}))

        server = headroom_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "summary", report=str(bogus)))
        assert out.startswith("error:")
        assert "not a headroom report" in out
