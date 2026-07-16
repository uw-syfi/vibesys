from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from vibesys.linux_cpu_profiler import (
    Capability,
    DiagnosticCode,
    LinuxProfilerTool,
    collect,
    detect_capability,
    summarize,
)


def test_detect_capability_rejects_non_linux():
    capability = detect_capability(system="Darwin")

    assert capability.tool is LinuxProfilerTool.NONE
    assert capability.diagnostics == (DiagnosticCode.NOT_LINUX,)


def test_detect_capability_reports_missing_perf():
    with patch("vibesys.linux_cpu_profiler._read_int", return_value=1):
        capability = detect_capability(system="Linux", which=lambda _name: None)

    assert capability.tool is LinuxProfilerTool.NONE
    assert DiagnosticCode.PERF_UNAVAILABLE in capability.diagnostics


def test_collect_persists_perf_artifacts_and_summary(tmp_path: Path):
    capability = Capability(
        tool=LinuxProfilerTool.PERF,
        perf_path="/usr/bin/perf",
        perf_version="perf version 6.8",
        perf_event_paranoid=1,
        kptr_restrict=0,
    )

    def fake_run(command: list[str], *, timeout: int | None):
        del timeout
        if command[1] == "stat":
            output = Path(command[command.index("-o") + 1])
            output.write_text(
                "1000,,cycles\n500,,instructions\n10,,context-switches\n1,,cpu-migrations\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1] == "record":
            output = Path(command[command.index("-o") + 1])
            output.write_bytes(b"perf data")
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1] == "report":
            return subprocess.CompletedProcess(
                command,
                0,
                "  65.00% bench queue-candidate.so [.] enqueue\n"
                "  20.00% bench queue-candidate.so [.] dequeue\n",
                "",
            )
        raise AssertionError(command)

    with patch("vibesys.linux_cpu_profiler._run_text", side_effect=fake_run):
        result = collect(["bench", "--scenario", "spsc"], tmp_path, capability=capability)

    assert result.status == "ok"
    assert result.stat_artifact == str(tmp_path / "perf-stat.csv")
    assert result.record_artifact == str(tmp_path / "perf.data")
    assert result.report_artifact == str(tmp_path / "perf-report.txt")
    assert result.metadata == str(tmp_path / "metadata.json")
    assert result.counters[0] == {"event": "cycles", "value": "1000", "unit": ""}
    assert "enqueue" in result.hot_symbols[0]
    assert "linux perf ok" in result.summary

    persisted = summarize(tmp_path)
    assert persisted["counters"][1]["event"] == "instructions"
    assert "dequeue" in persisted["hot_symbols"][1]


def test_collect_degrades_when_perf_unavailable(tmp_path: Path):
    capability = Capability(
        tool=LinuxProfilerTool.NONE,
        perf_path=None,
        perf_version=None,
        perf_event_paranoid=None,
        kptr_restrict=None,
        diagnostics=(DiagnosticCode.PERF_UNAVAILABLE,),
    )

    result = collect(["bench"], tmp_path, capability=capability)

    assert result.status == "error"
    assert result.stat_artifact is None
    assert result.record_artifact is None
    assert result.metadata == str(tmp_path / "metadata.json")
    assert DiagnosticCode.PERF_UNAVAILABLE in result.diagnostics
