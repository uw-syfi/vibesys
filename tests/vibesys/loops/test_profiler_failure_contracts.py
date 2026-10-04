"""Malformed reports and failed analyzer commands never become normal tool replies."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from mcp.server.fastmcp.exceptions import ToolError
from resources.profilers.headroom.server import build_server as headroom_server
from resources.profilers.ncu.server import build_server as ncu_server
from resources.profilers.neuron.server import build_server as neuron_server
from resources.profilers.nsys import analyze_nsys
from resources.profilers.nsys.server import build_server as nsys_server
from resources.profilers.rocprof import analyze_rocprof, capture, counters
from resources.profilers.rocprof.server import build_server as rocprof_server
from resources.profilers.torch import capture_ops


@pytest.mark.parametrize(
    "tool", ["tables", "kernels", "cpu_overhead", "idle_gaps", "memory", "graph_replays"]
)
def test_missing_nsys_report_is_a_tool_error_and_is_not_created(tmp_path: Path, tool: str) -> None:
    report = tmp_path / "absent.sqlite"
    with pytest.raises(ToolError, match="report does not exist"):
        asyncio.run(nsys_server().call_tool(tool, {"report": str(report)}))
    assert not report.exists()


@pytest.mark.parametrize("suffix", [".sqlite", ".nsys-rep"])
def test_missing_nsys_export_is_a_tool_error(tmp_path: Path, suffix: str) -> None:
    with pytest.raises(ToolError, match="report does not exist"):
        asyncio.run(
            nsys_server().call_tool("export", {"report": str(tmp_path / f"absent{suffix}")})
        )


@settings(max_examples=32)
@given(code=st.integers(min_value=1, max_value=255))
def test_every_nonzero_ncu_counter_set_exit_is_a_tool_error(code: int) -> None:
    with tempfile.TemporaryDirectory(dir="/dev/shm") as directory:
        executable = Path(directory) / "ncu"
        executable.write_text(
            '#!/bin/sh\nif [ "$1" = "--list-sets" ]; then exit '
            + str(code)
            + "; fi\necho sections\n"
        )
        executable.chmod(0o755)
        # Environment is command-bound input, not a patched dependency.
        with pytest.MonkeyPatch.context() as environment:
            environment.setenv("NCU_PATH", str(executable))
            with pytest.raises(ToolError, match=f"exited {code}"):
                asyncio.run(ncu_server().call_tool("list_sections_and_sets", {}))


@settings(max_examples=32)
@given(code=st.integers(min_value=1, max_value=255))
def test_every_nonzero_neuron_analysis_exit_is_a_tool_error(code: int) -> None:
    with tempfile.TemporaryDirectory(dir="/dev/shm") as directory:
        executable = Path(directory) / "neuron-explorer"
        executable.write_text(f"#!/bin/sh\necho analyzer_failure\nexit {code}\n")
        executable.chmod(0o755)
        with pytest.MonkeyPatch.context() as environment:
            environment.setenv("PATH", directory + os.pathsep + os.environ.get("PATH", ""))
            with pytest.raises(ToolError, match=f"code {code}"):
                asyncio.run(neuron_server().call_tool("summary", {"report": directory}))


@pytest.mark.parametrize("tool", ["summary", "summary_json", "operators", "dma", "view"])
def test_missing_neuron_report_is_a_tool_error(tmp_path: Path, tool: str) -> None:
    with pytest.raises(ToolError):
        asyncio.run(neuron_server().call_tool(tool, {"report": str(tmp_path / "missing" / "file")}))


def test_invalid_headroom_report_is_a_tool_error(tmp_path: Path) -> None:
    report = tmp_path / "headroom.json"
    report.write_text("invalid JSON")
    with pytest.raises(ToolError, match="not valid JSON"):
        asyncio.run(headroom_server().call_tool("summary", {"report": str(report)}))


_INVALID_NUMBERS = st.one_of(
    st.sampled_from(["nan", "inf", "-inf", "NaN", "bogus"]),
    st.text(alphabet="xyz", min_size=1, max_size=20),
)


@example(value="bogus", field="Counter_Value")
@example(value="nan", field="Start_Timestamp")
@example(value="inf", field="Grid_Size")
@given(
    value=_INVALID_NUMBERS,
    field=st.sampled_from(
        [
            "Counter_Value",
            "Start_Timestamp",
            "End_Timestamp",
            "Grid_Size",
            "Workgroup_Size",
            "LDS_Block_Size",
            "Scratch_Size",
            "VGPR_Count",
            "SGPR_Count",
        ]
    ),
)
def test_invalid_rocprof_counter_values_are_not_zero(value: str, field: str) -> None:
    with tempfile.TemporaryDirectory(dir="/dev/shm") as directory:
        path = Path(directory) / "out_counter_collection.csv"
        with path.open("w", newline="") as output:
            writer = csv.writer(output)
            row = {"Kernel_Name": "kernel", "Counter_Name": "SQ_INSTS_VALU", "Counter_Value": "1"}
            row[field] = value
            writer.writerow(row.keys())
            writer.writerow(row.values())
        with pytest.raises(RuntimeError) as failed:
            counters.cmd_report(
                argparse.Namespace(dirs=[directory], kernel=None, arch=None, top=15)
            )
        assert type(failed.value).__name__ == "CaptureFailedError"


@given(value=_INVALID_NUMBERS)
def test_invalid_rocprof_trace_timestamps_are_not_zero(value: str) -> None:
    with tempfile.TemporaryDirectory(dir="/dev/shm") as directory:
        path = Path(directory) / "out_kernel_trace.csv"
        with path.open("w", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(["Kernel_Name", "Start_Timestamp", "End_Timestamp"])
            writer.writerow(["kernel", value, "100"])
        with pytest.raises(RuntimeError) as failed:
            analyze_rocprof.cmd_kernels(argparse.Namespace(report=directory, top=15, window="all"))
        assert type(failed.value).__name__ == "CaptureFailedError"


def test_failed_torch_capture_is_a_typed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(tmp_path))
    with pytest.raises(RuntimeError) as failed:
        capture_ops.profile_ops(command="exit 17", inject=False, grace_s=0)
    assert type(failed.value).__name__ == "CaptureFailedError"
    assert getattr(failed.value, "status", None) == "target_failed"


def test_torch_capture_without_a_trace_is_a_typed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(tmp_path))
    with pytest.raises(RuntimeError) as failed:
        capture_ops.profile_ops(command="true", inject=False, grace_s=0)
    assert type(failed.value).__name__ == "CaptureFailedError"
    assert getattr(failed.value, "status", None) == "no_trace"


def test_corrupt_nsys_database_is_a_typed_failure(tmp_path: Path) -> None:
    report = tmp_path / "corrupt.sqlite"
    report.write_text("not a database")
    with pytest.raises(RuntimeError, match="invalid SQLite report") as failed:
        analyze_nsys.cmd_tables(argparse.Namespace(report=str(report)))
    assert type(failed.value).__name__ == "CaptureFailedError"


@given(
    payload=st.one_of(
        st.text(alphabet="xyz{", min_size=1, max_size=20),
        st.sampled_from(["[]", "null", "42", '"text"']),
    )
)
def test_invalid_rocprof_json_does_not_become_an_empty_report(payload: str) -> None:
    with tempfile.TemporaryDirectory(dir="/dev/shm") as directory:
        path = Path(directory) / "kernel_trace.json"
        path.write_text(payload)
        with pytest.raises(RuntimeError) as failed:
            analyze_rocprof.cmd_kernels(argparse.Namespace(report=directory, top=15, window="all"))
        assert type(failed.value).__name__ == "CaptureFailedError"


@pytest.mark.parametrize("tool", ["operators", "capture"])
def test_neuron_success_exit_without_valid_profile_is_a_tool_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    executable = tmp_path / "neuron-explorer"
    executable.write_text("#!/bin/sh\necho invalid_JSON\nexit 0\n")
    executable.chmod(0o755)
    (tmp_path / "profile.ntff").write_bytes(b"fixture")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    arguments = (
        {"report": str(tmp_path)}
        if tool == "operators"
        else {"workload": "true", "out_dir": str(tmp_path / "capture")}
    )
    with pytest.raises(ToolError):
        asyncio.run(neuron_server().call_tool(tool, arguments))


def test_counter_pass_setup_failure_is_a_typed_capture_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "rocminfo"
    executable.write_text('#!/bin/sh\n[ "$#" -eq 0 ] || exit 2\necho "Name: gfx942"\n')
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(tmp_path / "profiles"))
    lifecycle = capture.capture_runtime.Lifecycle(command="true", setup_command="exit 9")
    with pytest.raises(RuntimeError, match="setup_failed") as failed:
        capture.profile_counters(lifecycle, sets=["mfma"])
    assert type(failed.value).__name__ == "CaptureFailedError"


@given(
    field=st.sampled_from(["start_timestamp", "end_timestamp"]),
    omit=st.booleans(),
    report_format=st.sampled_from(["csv", "json"]),
)
def test_missing_required_rocprof_timestamps_never_become_zero(
    field: str, *, omit: bool, report_format: str
) -> None:
    with tempfile.TemporaryDirectory(dir="/dev/shm") as directory:
        row = {"name": "kernel", "start_timestamp": "10", "end_timestamp": "20"}
        if omit:
            del row[field]
        else:
            row[field] = ""
        if report_format == "json":
            path = Path(directory) / "kernel_trace.json"
            path.write_text(json.dumps({"buffer_records": {"kernel_dispatch": [row]}}))
        else:
            aliases = {
                "name": "Kernel_Name",
                "start_timestamp": "Start_Timestamp",
                "end_timestamp": "End_Timestamp",
            }
            path = Path(directory) / "out_kernel_trace.csv"
            with path.open("w", newline="") as output:
                writer = csv.writer(output)
                writer.writerow([aliases[key] for key in row])
                writer.writerow(row.values())
        with pytest.raises(RuntimeError, match="missing required numeric") as failed:
            analyze_rocprof.cmd_kernels(argparse.Namespace(report=directory, top=15, window="all"))
        assert type(failed.value).__name__ == "CaptureFailedError"


def test_nsys_schema_error_is_translated_at_the_analysis_boundary(tmp_path: Path) -> None:
    report = tmp_path / "schema.sqlite"
    with sqlite3.connect(report) as connection:
        connection.execute("CREATE TABLE CUPTI_ACTIVITY_KIND_RUNTIME (nameId INTEGER)")
    with pytest.raises(ToolError) as failed:
        asyncio.run(nsys_server().call_tool("cpu_overhead", {"report": str(report)}))
    assert type(failed.value.__cause__).__name__ == "CaptureFailedError"


def test_busy_capture_is_a_tool_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(tmp_path))
    runtime = capture.capture_runtime
    with (
        runtime.exclusive_capture("timeline", "already-running"),
        pytest.raises(ToolError, match="busy: capture") as failed,
    ):
        asyncio.run(rocprof_server().call_tool("profile_timeline", {"command": "true"}))
    assert type(failed.value.__cause__).__name__ == "CaptureBusyError"
