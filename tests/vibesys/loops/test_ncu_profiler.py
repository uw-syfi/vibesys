"""NCU profiler tools read reports without running candidate code."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from resources.profilers.ncu.server import build_server

if TYPE_CHECKING:
    from pathlib import Path


async def _call(name: str, **arguments: object) -> dict[str, object]:
    _, structured = await build_server().call_tool(name, arguments)
    return cast("dict[str, object]", structured)


def test_ncu_server_registers_discovery_and_report_tools() -> None:
    names = {tool.name for tool in asyncio.run(build_server().list_tools())}
    assert names == {
        "capabilities",
        "list_sections_and_sets",
        "query_metrics",
        "read_report",
    }


def test_ncu_server_reports_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NCU_PATH", "/missing/ncu")
    with pytest.raises(ToolError, match="NCU_PATH"):
        asyncio.run(_call("capabilities"))


def test_ncu_server_reads_existing_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ncu = tmp_path / "ncu"
    ncu.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    ncu.chmod(0o755)
    report = tmp_path / "sample.ncu-rep"
    report.write_bytes(b"fixture")
    monkeypatch.setenv("NCU_PATH", str(ncu))

    result = asyncio.run(_call("read_report", path=str(report), page="raw"))
    assert result["status"] == "ok"
    assert result["report"] == str(report)
    assert "--import" in str(result["output"])
    assert str(report) in str(result["output"])
    assert result["truncated"] is False


def test_ncu_server_rejects_missing_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NCU_PATH", "/missing/ncu")
    with pytest.raises(ToolError, match=r"missing\.ncu-rep"):
        asyncio.run(_call("read_report", path=str(tmp_path / "missing.ncu-rep")))


def test_ncu_server_filters_large_raw_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ncu = tmp_path / "ncu"
    ncu.write_text(
        "#!/bin/sh\n"
        "i=0\n"
        'while [ "$i" -lt 2500 ]; do echo unrelated_metric; i=$((i + 1)); done\n'
        "echo sm__target_metric,42\n"
    )
    ncu.chmod(0o755)
    report = tmp_path / "sample.ncu-rep"
    report.write_bytes(b"fixture")
    monkeypatch.setenv("NCU_PATH", str(ncu))

    result = asyncio.run(
        _call("read_report", path=str(report), page="raw", contains="target_metric")
    )
    assert result["status"] == "ok"
    assert result["matching_lines"] == 1
    assert result["output"] == "sm__target_metric,42"
    assert result["truncated"] is False
