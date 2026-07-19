from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from vibesys.linux_cpu_profiler import (
    Capability,
    DiagnosticCode,
    LinuxProfilerTool,
    _extract_hot_symbols,
    _parse_perf_stat_csv,
    _read_int,
    collect,
    detect_capability,
    parse_command,
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


def test_read_int_returns_none_for_missing_or_invalid_values(tmp_path: Path):
    invalid = tmp_path / "not-an-int"
    invalid.write_text("restricted\n", encoding="utf-8")

    assert _read_int(invalid) is None
    assert _read_int(tmp_path / "missing") is None


def test_detect_capability_reports_restrictions_and_stat_failure():
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs):
        calls.append(command)
        if command[1] == "--version":
            return subprocess.CompletedProcess(command, 0, "perf version 6.8\n", "")
        if command[1] == "stat":
            return subprocess.CompletedProcess(command, 255, "", "No permission")
        raise AssertionError(command)

    with patch("vibesys.linux_cpu_profiler._read_int", side_effect=[3, 1]):
        capability = detect_capability(
            system="Linux",
            which=lambda _name: "/usr/bin/perf",
            run=fake_run,
        )

    assert calls[0] == ["/usr/bin/perf", "--version"]
    assert capability.tool is LinuxProfilerTool.PERF
    assert capability.perf_version == "perf version 6.8"
    assert DiagnosticCode.PERF_EVENT_PARANOID_RESTRICTIVE in capability.diagnostics
    assert DiagnosticCode.KERNEL_SYMBOLS_RESTRICTED in capability.diagnostics
    assert DiagnosticCode.PERF_STAT_UNAVAILABLE in capability.diagnostics


def test_detect_capability_handles_perf_version_exception():
    def fake_run(_command: list[str], **_kwargs):
        raise OSError("cannot execute perf")

    with patch("vibesys.linux_cpu_profiler._read_int", return_value=None):
        capability = detect_capability(
            system="Linux",
            which=lambda _name: "/usr/bin/perf",
            run=fake_run,
        )

    assert capability.tool is LinuxProfilerTool.NONE
    assert capability.perf_path == "/usr/bin/perf"
    assert DiagnosticCode.PERF_UNAVAILABLE in capability.diagnostics


def test_detect_capability_handles_perf_stat_exception():
    def fake_run(command: list[str], **_kwargs):
        if command[1] == "--version":
            return subprocess.CompletedProcess(command, 0, "", "perf version 6.9\n")
        raise subprocess.TimeoutExpired(command, 10)

    with patch("vibesys.linux_cpu_profiler._read_int", return_value=None):
        capability = detect_capability(
            system="Linux",
            which=lambda _name: "/usr/bin/perf",
            run=fake_run,
        )

    assert capability.tool is LinuxProfilerTool.PERF
    assert capability.perf_version == "perf version 6.9"
    assert DiagnosticCode.PERF_STAT_UNAVAILABLE in capability.diagnostics


def test_perf_parsers_skip_malformed_rows_and_stop_at_limit(tmp_path: Path):
    stat_path = tmp_path / "perf-stat.csv"
    stat_path.write_text(
        "# ignored\ntoo-short\n# comment,,cycles\n10,,cycles\n",
        encoding="utf-8",
    )
    report_text = (
        "# header\n"
        "not-a-percent bench [.] setup\n"
        "  55.00% bench libqueue.so [.] enqueue\n"
        "  30.00% bench libqueue.so [.] dequeue\n"
    )

    assert _parse_perf_stat_csv(tmp_path / "missing.csv") == ()
    assert _parse_perf_stat_csv(stat_path) == ({"event": "cycles", "value": "10", "unit": ""},)
    assert _extract_hot_symbols(report_text, limit=1) == ("55.00% bench libqueue.so [.] enqueue",)


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


def test_collect_reports_failed_stat_record_and_missing_counters(tmp_path: Path):
    capability = Capability(
        tool=LinuxProfilerTool.PERF,
        perf_path="/usr/bin/perf",
        perf_version="perf version 6.8",
        perf_event_paranoid=None,
        kptr_restrict=None,
    )

    def fake_run(command: list[str], *, timeout: int | None):
        del timeout
        if command[1] == "stat":
            return subprocess.CompletedProcess(command, 255, "", "stat failed")
        if command[1] == "record":
            raise subprocess.TimeoutExpired(command, 3)
        raise AssertionError(command)

    with patch("vibesys.linux_cpu_profiler._run_text", side_effect=fake_run):
        result = collect(["bench"], tmp_path, capability=capability, timeout=3)

    assert result.status == "error"
    assert result.counters == ()
    assert "no perf stat counters parsed" in result.summary
    assert DiagnosticCode.PERF_STAT_UNAVAILABLE in result.diagnostics
    assert DiagnosticCode.PERF_RECORD_UNAVAILABLE in result.diagnostics
    assert DiagnosticCode.COLLECTION_FAILED in result.diagnostics


def test_collect_reports_failed_perf_report(tmp_path: Path):
    capability = Capability(
        tool=LinuxProfilerTool.PERF,
        perf_path="/usr/bin/perf",
        perf_version="perf version 6.8",
        perf_event_paranoid=None,
        kptr_restrict=None,
    )

    def fake_run(command: list[str], *, timeout: int | None):
        del timeout
        if command[1] == "stat":
            Path(command[command.index("-o") + 1]).write_text("1,,cycles\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1] == "record":
            Path(command[command.index("-o") + 1]).write_bytes(b"perf data")
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1] == "report":
            return subprocess.CompletedProcess(command, 1, "", "report failed")
        raise AssertionError(command)

    with patch("vibesys.linux_cpu_profiler._run_text", side_effect=fake_run):
        result = collect(["bench"], tmp_path, capability=capability)

    assert result.status == "error"
    assert result.report_artifact == str(tmp_path / "perf-report.txt")
    assert DiagnosticCode.PERF_REPORT_UNAVAILABLE in result.diagnostics
    assert DiagnosticCode.COLLECTION_FAILED in result.diagnostics


def test_summarize_empty_directory_and_parse_command(tmp_path: Path):
    summary = summarize(tmp_path)

    assert summary["metadata"] is None
    assert summary["summary"].startswith("linux perf ok; counters: no perf stat counters parsed")
    assert parse_command("bench --scenario 'spsc queue'") == ["bench", "--scenario", "spsc queue"]


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
