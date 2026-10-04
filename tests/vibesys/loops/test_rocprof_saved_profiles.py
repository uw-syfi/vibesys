"""Saved captures retain the workload-success contract through every MCP reader."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from mcp.server.fastmcp.exceptions import ToolError

_REPO = Path(__file__).resolve().parents[3]
_SUCCESS = {"ok", "killed_after_grace"}


@pytest.fixture(scope="module")
def server_mod() -> ModuleType:
    path = _REPO / "resources/profilers/rocprof/server.py"
    spec = importlib.util.spec_from_file_location("_saved_rocprof_server", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


def _saved_timeline(root: Path, name: str, status: object) -> Path:
    directory = root / name
    directory.mkdir()
    (directory / "out_kernel_trace.csv").write_text(
        "Kernel_Name,Dispatch_Id,Start_Timestamp,End_Timestamp\n"
        "saved_workload_kernel,1,10000,20000\n"
    )
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "kind": "timeline",
                "capture_id": name,
                "status": status,
                "load_returncode": 0 if isinstance(status, str) and status in _SUCCESS else 1,
            }
        )
    )
    return directory


@pytest.mark.parametrize("status", ["ok", "killed_after_grace"])
def test_saved_success_remains_readable(
    server_mod: ModuleType, tmp_path: Path, status: str
) -> None:
    capture = _saved_timeline(tmp_path, "successful", status)
    server = server_mod.build_server()
    for tool, arguments in (
        ("summary", {"capture_id": str(capture)}),
        ("compare", {"a": str(capture), "b": str(capture)}),
        ("kernels", {"report": str(capture)}),
    ):
        _, result = asyncio.run(server.call_tool(tool, arguments))
        assert "saved_workload_kernel" in result["result"]


@pytest.mark.parametrize(
    "reader",
    ["summary", "compare_baseline", "compare_candidate", "kernels_root", "kernels_artifact"],
)
@settings(max_examples=15)
@given(
    status=st.one_of(
        st.none(),
        st.text().filter(lambda value: value not in _SUCCESS),
        st.integers(),
        st.lists(st.integers(), max_size=3),
        st.dictionaries(st.text(max_size=5), st.integers(), max_size=3),
    )
)
@example(status="load_failed")
@example(status="target_failed")
@example(status="not_ready")
@example(status="timed_out")
@example(status="cancelled")
@example(status="setup_failed")
@example(status=[])
@example(status={})
def test_saved_non_success_never_returns_profile_analysis(
    server_mod: ModuleType,
    tmp_path_factory: pytest.TempPathFactory,
    status: object,
    reader: str,
) -> None:
    root = tmp_path_factory.mktemp("saved_gate")
    failed = _saved_timeline(root, "failed", status)
    successful = _saved_timeline(root, "successful", "ok")
    server = server_mod.build_server()
    calls = {
        "summary": ("summary", {"capture_id": str(failed)}),
        "compare_baseline": ("compare", {"a": str(failed), "b": str(successful)}),
        "compare_candidate": ("compare", {"a": str(successful), "b": str(failed)}),
        "kernels_root": ("kernels", {"report": str(failed)}),
        "kernels_artifact": ("kernels", {"report": str(failed / "out_kernel_trace.csv")}),
    }
    tool, arguments = calls[reader]
    with pytest.raises(ToolError, match="capture failed") as error:
        asyncio.run(server.call_tool(tool, arguments))
    assert f"({status})" in str(error.value)
    assert "load_rc=1" in str(error.value)


@pytest.mark.parametrize("kind", ["timeline", "counters", "kernel_deep", "instructions", "ops"])
@pytest.mark.parametrize("use_id", [False, True])
def test_saved_failure_gates_summary_and_drilldowns_for_each_kind(
    server_mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    *,
    use_id: bool,
) -> None:
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(tmp_path))
    capture = _saved_timeline(tmp_path, "failed", "load_failed")
    artifact = capture / "artifact"
    artifact.mkdir()
    (capture / "manifest.json").write_text(
        json.dumps(
            {
                "kind": kind,
                "status": "load_failed",
                "load_returncode": 1,
                "set_dirs": {"l2": str(artifact)},
                "meta": {"workload_dir": str(artifact)},
                "primary_trace": "out_kernel_trace.csv",
            }
        )
    )
    ref = capture.name if use_id else str(capture)
    drilldowns = {
        "timeline": ("kernels", {"report": ref}),
        "counters": ("counter_report", {"dirs": [ref]}),
        "kernel_deep": ("compute_analyze", {"workload_dir": ref}),
        "instructions": ("att_hotspots", {"dispatch_dir": ref}),
        "ops": ("certify", {"trace": ref}),
    }
    nested_drilldowns = {
        "timeline": ("kernels", {"report": str(capture / "out_kernel_trace.csv")}),
        "counters": ("counter_report", {"dirs": [str(artifact)]}),
        "kernel_deep": ("compute_analyze", {"workload_dir": str(artifact)}),
        "instructions": ("att_hotspots", {"dispatch_dir": str(artifact)}),
        "ops": ("certify", {"trace": str(capture / "out_kernel_trace.csv")}),
    }
    server = server_mod.build_server()
    for tool, arguments in (
        ("summary", {"capture_id": ref}),
        drilldowns[kind],
        nested_drilldowns[kind],
    ):
        with pytest.raises(ToolError, match=r"capture failed \(load_failed\)"):
            asyncio.run(server.call_tool(tool, arguments))
    assert (capture / "out_kernel_trace.csv").is_file()


def test_missing_status_is_not_legacy_success(server_mod: ModuleType, tmp_path: Path) -> None:
    capture = _saved_timeline(tmp_path, "missing_status", "ok")
    (capture / "manifest.json").write_text(json.dumps({"kind": "timeline"}))
    server = server_mod.build_server()
    with pytest.raises(ToolError, match="capture failed"):
        asyncio.run(server.call_tool("summary", {"capture_id": str(capture)}))


def test_raw_report_with_unrelated_parent_manifest_remains_readable(
    server_mod: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps({"dataset": "audio"}))
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "out_kernel_trace.csv").write_text(
        "Kernel_Name,Dispatch_Id,Start_Timestamp,End_Timestamp\nraw_workload_kernel,1,10000,20000\n"
    )
    server = server_mod.build_server()
    _, result = asyncio.run(server.call_tool("kernels", {"report": str(raw)}))
    assert "raw_workload_kernel" in result["result"]


@pytest.mark.parametrize("direction", ["inside_to_raw", "outside_to_failed"])
def test_saved_failure_cannot_be_bypassed_with_a_symlink(
    server_mod: ModuleType, tmp_path: Path, direction: str
) -> None:
    failed = _saved_timeline(tmp_path, "failed", "load_failed")
    raw = tmp_path / "raw.csv"
    raw.write_text((failed / "out_kernel_trace.csv").read_text())
    if direction == "inside_to_raw":
        link = failed / "linked_trace.csv"
        link.symlink_to(raw)
    else:
        link = tmp_path / "linked_trace.csv"
        link.symlink_to(failed / "out_kernel_trace.csv")
    server = server_mod.build_server()
    with pytest.raises(ToolError, match=r"capture failed \(load_failed\)"):
        asyncio.run(server.call_tool("kernels", {"report": str(link)}))


@pytest.mark.parametrize("reader", ["summary", "compare", "counter_report", "certify"])
def test_saved_capture_directory_symlink_retains_parent_failure(
    server_mod: ModuleType, tmp_path: Path, reader: str
) -> None:
    failed = _saved_timeline(tmp_path, "failed", "load_failed")
    successful = _saved_timeline(tmp_path, "successful", "ok")
    link = failed / "linked_capture"
    link.symlink_to(successful, target_is_directory=True)
    arguments = {
        "summary": {"capture_id": str(link)},
        "compare": {"a": str(link), "b": str(successful)},
        "counter_report": {"dirs": [str(link)]},
        "certify": {"trace": str(link)},
    }
    server = server_mod.build_server()
    with pytest.raises(ToolError, match=r"capture failed \(load_failed\)"):
        asyncio.run(server.call_tool(reader, arguments[reader]))
