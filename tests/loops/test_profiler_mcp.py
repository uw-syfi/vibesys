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

import pytest

from vibesys.loops.profiler import mcp_spec
from vibesys.profilers import ProfilerKind


# The servers live under resources/ (co-located with the analysis scripts) so
# importing them by file path keeps the tests decoupled from sys.path state.
def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
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
        spec.loader.exec_module(module)
    finally:
        if inserted:
            sys.path.remove(parent)
    return module


_REPO = Path(__file__).resolve().parent.parent.parent


def test_profiler_mcp_spec_maps_known_kinds_exactly():
    assert mcp_spec(ProfilerKind.NONE) is None

    nsys = mcp_spec(ProfilerKind.NSYS)
    assert nsys.name == "vibesys-nsys-profiler"
    assert nsys.args == ["nsys_profiler/server.py"]

    torch = mcp_spec(ProfilerKind.TORCH)
    assert torch.name == "vibesys-torch-profiler"
    assert torch.args == ["torch_profiler/server.py"]

    neuron = mcp_spec(ProfilerKind.NEURON)
    assert neuron.name == "vibesys-neuron-profiler"
    assert neuron.args == ["neuron_profiler/server.py"]

    otel = mcp_spec(ProfilerKind.OTEL)
    assert otel.name == "vibesys-otel-profiler"
    assert otel.args == ["otel_profiler/server.py"]

    macos = mcp_spec(ProfilerKind.MACOS_CPU)
    assert macos.name == "vibesys-macos-cpu-profiler"
    assert macos.args == ["macos_cpu_profiler/server.py"]


def test_profiler_mcp_spec_rejects_unknown_kind():
    with pytest.raises(TypeError, match="ProfilerKind"):
        mcp_spec("bogus")


@pytest.fixture(scope="module")
def nsys_server_mod():
    return _load_module(
        "_nsys_server",
        _REPO / "resources" / "profilers" / "nsys" / "server.py",
    )


@pytest.fixture(scope="module")
def torch_server_mod():
    return _load_module(
        "_torch_server",
        _REPO / "resources" / "profilers" / "torch" / "server.py",
    )


@pytest.fixture(scope="module")
def otel_server_mod():
    return _load_module(
        "_otel_server",
        _REPO / "resources" / "profilers" / "otel" / "server.py",
    )


async def _list_tool_names(server) -> set[str]:
    tools = await server.list_tools()
    return {t.name for t in tools}


async def _call_tool(server, name: str, **kwargs) -> str:
    _, structured = await server.call_tool(name, kwargs)
    return structured["result"]


# ---------------------------------------------------------------------------
# nsys MCP server
# ---------------------------------------------------------------------------


class TestNsysMcpServer:
    def test_registers_expected_tools(self, nsys_server_mod):
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

    def test_tables_tool_reports_empty_db(self, nsys_server_mod, tmp_path):
        """Against an empty SQLite file, ``tables`` returns a no-output marker."""
        db = tmp_path / "empty.sqlite"
        sqlite3.connect(str(db)).close()

        server = nsys_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "tables", report=str(db)))
        # No tables → the cmd_tables body prints nothing; the wrapper
        # coerces that to "(no output)".
        assert out == "(no output)"

    def test_kernels_tool_reports_no_data(self, nsys_server_mod, tmp_path):
        """A SQLite file without a CUPTI_ACTIVITY_KIND_KERNEL table returns a friendly message."""
        db = tmp_path / "nokernels.sqlite"
        sqlite3.connect(str(db)).close()

        server = nsys_server_mod.build_server()
        out = asyncio.run(_call_tool(server, "kernels", report=str(db)))
        assert "No kernel data" in out

    def test_query_tool_runs_arbitrary_sql(self, nsys_server_mod, tmp_path):
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
    def test_registers_expected_tools(self, otel_server_mod):
        server = otel_server_mod.build_server()
        names = asyncio.run(_list_tool_names(server))
        assert names == {"reports", "summary", "compare"}

    def test_summary_and_compare_use_normalized_service_rows(self, otel_server_mod, tmp_path):
        before = tmp_path / "before.json"
        after = tmp_path / "after.json"
        before.write_text(json.dumps(_otel_report(20.0)))
        after.write_text(json.dumps(_otel_report(12.0)))

        summary = otel_server_mod.summarize_report(str(after))
        comparison = otel_server_mod.compare_reports(str(before), str(after))

        assert summary["span_count"] == 4
        assert summary["services_by_p95"][0]["name"] == "frontend"
        assert comparison["service_p95_changes"][0] == {
            "name": "frontend",
            "before_p95_ms": 20.0,
            "after_p95_ms": 12.0,
            "delta_p95_ms": -8.0,
            "delta_percent": -40.0,
        }
        assert comparison["span_p95_changes"][0]["name"] == "frontend:GET /hotels"
        assert comparison["span_p95_changes"][0]["delta_p95_ms"] == -8.0
        assert comparison["datastore_p95_changes"][0]["name"] == "frontend:db"
        assert comparison["datastore_p95_changes"][0]["delta_p95_ms"] == -8.0
        assert otel_server_mod.find_reports(str(tmp_path)) == [
            after.as_posix(),
            before.as_posix(),
        ]

    @pytest.mark.parametrize("identity_field", ["workload_name", "workload_hash"])
    def test_compare_rejects_incompatible_reports(self, otel_server_mod, tmp_path, identity_field):
        before = tmp_path / "before.json"
        after = tmp_path / "after.json"
        before.write_text(json.dumps(_otel_report(20.0)))
        after_report = _otel_report(12.0)
        after_report[identity_field] = "different"
        after.write_text(json.dumps(after_report))

        with pytest.raises(ValueError, match="matching workload identity"):
            otel_server_mod.compare_reports(str(before), str(after))

    def test_compare_allows_run_specific_measurement_timestamps(self, otel_server_mod, tmp_path):
        before = tmp_path / "before.json"
        after = tmp_path / "after.json"
        before.write_text(json.dumps(_otel_report(20.0)))
        after_report = _otel_report(12.0)
        after_report["measurement_windows"] = [
            {"start": "2026-07-23T12:00:00Z", "end": "2026-07-23T12:00:01Z"}
        ]
        after.write_text(json.dumps(after_report))

        comparison = otel_server_mod.compare_reports(str(before), str(after))

        assert comparison["service_p95_changes"][0]["delta_p95_ms"] == -8.0

    def test_load_report_rejects_invalid_aggregate_error_count(self, otel_server_mod, tmp_path):
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
    def test_load_report_rejects_malformed_contract(self, otel_server_mod, tmp_path, mutate):
        report = _otel_report(20.0)
        mutate(report)
        path = tmp_path / "invalid.json"
        path.write_text(json.dumps(report))

        with pytest.raises(ValueError):
            otel_server_mod.load_report(str(path))

    def test_summary_rejects_non_positive_top(self, otel_server_mod, tmp_path):
        path = tmp_path / "report.json"
        path.write_text(json.dumps(_otel_report(20.0)))

        with pytest.raises(ValueError, match="top must be positive"):
            otel_server_mod.summarize_report(str(path), top=0)

    def test_compare_rejects_non_positive_top_before_reading_files(self, otel_server_mod):
        # top is validated before any file I/O, so unreadable paths do not matter.
        with pytest.raises(ValueError, match="top must be positive"):
            otel_server_mod.compare_reports("missing-before.json", "missing-after.json", top=0)

    def test_find_reports_skips_hostile_json(self, otel_server_mod, tmp_path):
        valid = tmp_path / "valid.json"
        valid.write_text(json.dumps(_otel_report(20.0)))
        # A candidate under evaluation controls workspace files; none of these
        # may abort discovery of the valid report.
        (tmp_path / "binary.json").write_bytes(b"\xff\xfe\x00\x01 not valid utf-8")
        (tmp_path / "nested.json").write_text("[" * 3000 + "]" * 3000)
        (tmp_path / "bigint.json").write_text('{"schema_version": ' + "9" * 5000 + "}")
        (tmp_path / "truncated.json").write_text('{"schema_version": 1')

        assert otel_server_mod.find_reports(str(tmp_path)) == [valid.as_posix()]

    def test_compare_surfaces_rows_present_in_one_report(self, otel_server_mod, tmp_path):
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

        changes = otel_server_mod.compare_reports(str(before), str(after))["service_p95_changes"]
        by_name = {change["name"]: change for change in changes}

        assert set(by_name) == {"frontend", "checkout", "newsvc"}
        # A row new to the after report is surfaced with no baseline...
        assert by_name["newsvc"]["before_p95_ms"] is None
        assert by_name["newsvc"]["after_p95_ms"] == 500.0
        assert by_name["newsvc"]["delta_p95_ms"] is None
        assert by_name["newsvc"]["delta_percent"] is None
        # ...and a row absent from the after ranking is surfaced too.
        assert by_name["checkout"]["before_p95_ms"] == 8.0
        assert by_name["checkout"]["after_p95_ms"] is None
        # The largest-magnitude row ranks first even without a delta.
        assert changes[0]["name"] == "newsvc"
        assert by_name["frontend"]["delta_p95_ms"] == 0.0


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


# ---------------------------------------------------------------------------
# torch MCP server
# ---------------------------------------------------------------------------


class TestTorchMcpServer:
    def test_registers_expected_tools(self, torch_server_mod):
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

    def test_tables_tool_reports_prof_json_overview(self, torch_server_mod, tmp_path):
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

    def test_kernels_tool_ranks_by_self_cuda(self, torch_server_mod, tmp_path):
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
