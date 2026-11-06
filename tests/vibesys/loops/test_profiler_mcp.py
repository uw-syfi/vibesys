"""Tests for the profiler MCP servers.

We verify tool registration via ``FastMCP.list_tools`` and exercise a few
tools end-to-end through ``FastMCP.call_tool``. The stdio JSON-RPC framing
itself is the ``mcp`` package's responsibility.

Guarantees under test:

* Each profiler MCP server (nsys, torch, headroom, rocprof) registers its documented
  tools; each tool returns the documented summary on a small report and a clean error text for
  empty, hostile or malformed input.
* Capture tools of the rocprof server report busy, unknown ids and unknown sets as tool
  errors, and capability reports name the fix for what is missing.
"""

import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import textwrap
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import ModuleType
from typing import Protocol

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from tests.vibesys.loops.profiler_process_world import ScriptedHost


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


@pytest.fixture
def no_host_tools(rocprof_server_mod: ModuleType) -> Iterator[ScriptedHost]:
    """A host on which no ROCm tool or interpreter answers a capability query."""
    host = ScriptedHost()
    with rocprof_server_mod.capture_runtime.use_host(host):
        yield host


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
        with pytest.raises(ToolError, match="not a raw Kineto/Chrome trace"):
            asyncio.run(_call_tool(server, "certify", trace=str(prof)))

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
        with pytest.raises(ToolError, match="not a headroom report"):
            asyncio.run(_call_tool(server, "summary", report=str(bogus)))


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
            "profile_configured_timeline",
            "submit_configured_timeline",
            "capture_status",
            "await_capture",
            "cancel_capture",
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

    @pytest.mark.usefixtures("no_host_tools")
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

    @pytest.mark.usefixtures("no_host_tools")
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
        log.write_text("wall_ms: 12.5\nwall_ms: 13.0\nwall_ms_done: 2\n")

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

    @pytest.mark.usefixtures("no_host_tools")
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

        with pytest.raises(ToolError, match="capture not found"):
            asyncio.run(_call_tool(server, "summary", capture_id="bogus-capture-id"))
        with pytest.raises(ToolError, match="capture not found"):
            asyncio.run(_call_tool(server, "compare", a="bogus-a", b="bogus-b"))

    def test_profile_counters_tool_with_an_unknown_set_returns_error_string(
        self, rocprof_server_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(tmp_path / ".profiles"))
        server = rocprof_server_mod.build_server()

        with pytest.raises(ToolError):
            asyncio.run(
                _call_tool(server, "profile_counters", command="true", sets=["bogus-set-xyz"])
            )
