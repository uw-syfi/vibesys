from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Never, cast

import pytest

from vs_runtime.api.infrastructure import (
    LinuxProfilerCapability,
    LinuxProfilerDiagnostic,
    LinuxProfilerEffects,
    LinuxProfilerTool,
    NativeCpuProfilerKind,
    collect_linux_profile,
    detect_linux_profiler,
    parse_profile_command,
    preflight_native_cpu_profiler,
    summarize_linux_profile,
)


def test_detect_capability_rejects_non_linux() -> None:
    capability = detect_linux_profiler(system="Darwin")

    assert capability.tool is LinuxProfilerTool.NONE
    assert capability.diagnostics == (LinuxProfilerDiagnostic.NOT_LINUX,)


def test_detect_capability_reports_missing_perf() -> None:
    capability = detect_linux_profiler(
        system="Linux", which=lambda _name: None, read_int=lambda _path: 1
    )

    assert capability.tool is LinuxProfilerTool.NONE
    assert LinuxProfilerDiagnostic.PERF_UNAVAILABLE in capability.diagnostics


def test_preflight_marks_perf_stat_failure_unusable() -> None:
    capability = LinuxProfilerCapability(
        LinuxProfilerTool.PERF,
        "/usr/bin/perf",
        "perf version 6.8",
        3,
        1,
        (LinuxProfilerDiagnostic.PERF_STAT_UNAVAILABLE,),
    )

    result = preflight_native_cpu_profiler(
        NativeCpuProfilerKind.LINUX, detect_linux=lambda: capability
    )

    assert not result.usable
    assert result.selected_tool == "perf"
    assert result.details == (
        "perf_path=/usr/bin/perf",
        "perf_event_paranoid=3",
        "kptr_restrict=1",
    )


def test_preflight_rejects_unparsed_profiler_kind() -> None:
    with pytest.raises(TypeError, match="NativeCpuProfilerKind"):
        preflight_native_cpu_profiler(cast("NativeCpuProfilerKind", "linux"))


def test_detect_capability_reports_restrictions_and_stat_failure() -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1] == "--version":
            return subprocess.CompletedProcess(command, 0, "perf version 6.8\n", "")
        if command[1] == "stat":
            return subprocess.CompletedProcess(command, 255, "", "No permission")
        raise AssertionError(command)

    restrictions = iter((3, 1))
    capability = detect_linux_profiler(
        system="Linux",
        which=lambda _name: "/usr/bin/perf",
        run=fake_run,
        read_int=lambda _path: next(restrictions),
    )

    assert calls[0] == ["/usr/bin/perf", "--version"]
    assert capability.tool is LinuxProfilerTool.PERF
    assert capability.perf_version == "perf version 6.8"
    assert LinuxProfilerDiagnostic.PERF_EVENT_PARANOID_RESTRICTIVE in capability.diagnostics
    assert LinuxProfilerDiagnostic.KERNEL_SYMBOLS_RESTRICTED in capability.diagnostics
    assert LinuxProfilerDiagnostic.PERF_STAT_UNAVAILABLE in capability.diagnostics


def test_detect_capability_handles_perf_version_exception() -> None:
    def fake_run(_command: list[str], **_kwargs: object) -> Never:
        _failure_message = "cannot execute perf"
        raise OSError(_failure_message)

    capability = detect_linux_profiler(
        system="Linux",
        which=lambda _name: "/usr/bin/perf",
        run=fake_run,
        read_int=lambda _path: None,
    )

    assert capability.tool is LinuxProfilerTool.NONE
    assert capability.perf_path == "/usr/bin/perf"
    assert LinuxProfilerDiagnostic.PERF_UNAVAILABLE in capability.diagnostics


def test_detect_capability_handles_perf_stat_exception() -> None:
    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1] == "--version":
            return subprocess.CompletedProcess(command, 0, "", "perf version 6.9\n")
        raise subprocess.TimeoutExpired(command, 10)

    capability = detect_linux_profiler(
        system="Linux",
        which=lambda _name: "/usr/bin/perf",
        run=fake_run,
        read_int=lambda _path: None,
    )

    assert capability.tool is LinuxProfilerTool.PERF
    assert capability.perf_version == "perf version 6.9"
    assert LinuxProfilerDiagnostic.PERF_STAT_UNAVAILABLE in capability.diagnostics


def test_summary_skips_malformed_perf_rows_and_parses_hot_symbols(tmp_path: Path) -> None:
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

    (tmp_path / "perf-report.txt").write_text(report_text, encoding="utf-8")
    summary = summarize_linux_profile(tmp_path)

    assert summary["counters"] == [{"event": "cycles", "value": "10", "unit": ""}]
    assert summary["hot_symbols"][:1] == ["55.00% bench libqueue.so [.] enqueue"]


def test_collect_persists_perf_artifacts_and_summary(tmp_path: Path) -> None:
    capability = LinuxProfilerCapability(
        tool=LinuxProfilerTool.PERF,
        perf_path="/usr/bin/perf",
        perf_version="perf version 6.8",
        perf_event_paranoid=1,
        kptr_restrict=0,
    )

    def fake_run(command: list[str], *, timeout: int | None) -> subprocess.CompletedProcess[str]:
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

    result = collect_linux_profile(
        ["bench", "--scenario", "spsc"],
        tmp_path,
        capability=capability,
        effects=LinuxProfilerEffects(run=fake_run, now=lambda: 100.0),
    )

    assert result.status == "ok"
    assert result.stat_artifact == str(tmp_path / "perf-stat.csv")
    assert result.record_artifact == str(tmp_path / "perf.data")
    assert result.report_artifact == str(tmp_path / "perf-report.txt")
    assert result.metadata == str(tmp_path / "metadata.json")
    assert result.counters[0] == {"event": "cycles", "value": "1000", "unit": ""}
    assert "enqueue" in result.hot_symbols[0]
    assert "linux perf ok" in result.summary

    persisted = summarize_linux_profile(tmp_path)
    assert persisted["counters"][1]["event"] == "instructions"
    assert "dequeue" in persisted["hot_symbols"][1]


def test_collect_reports_failed_stat_record_and_missing_counters(tmp_path: Path) -> None:
    capability = LinuxProfilerCapability(
        tool=LinuxProfilerTool.PERF,
        perf_path="/usr/bin/perf",
        perf_version="perf version 6.8",
        perf_event_paranoid=None,
        kptr_restrict=None,
    )

    def fake_run(command: list[str], *, timeout: int | None) -> subprocess.CompletedProcess[str]:
        del timeout
        if command[1] == "stat":
            return subprocess.CompletedProcess(command, 255, "", "stat failed")
        if command[1] == "record":
            raise subprocess.TimeoutExpired(command, 3)
        raise AssertionError(command)

    result = collect_linux_profile(
        ["bench"],
        tmp_path,
        capability=capability,
        timeout=3,
        effects=LinuxProfilerEffects(run=fake_run),
    )

    assert result.status == "error"
    assert result.counters == ()
    assert "no perf stat counters parsed" in result.summary
    assert LinuxProfilerDiagnostic.PERF_STAT_UNAVAILABLE in result.diagnostics
    assert LinuxProfilerDiagnostic.PERF_RECORD_UNAVAILABLE in result.diagnostics
    assert LinuxProfilerDiagnostic.COLLECTION_FAILED in result.diagnostics


def test_collect_reports_failed_perf_report(tmp_path: Path) -> None:
    capability = LinuxProfilerCapability(
        tool=LinuxProfilerTool.PERF,
        perf_path="/usr/bin/perf",
        perf_version="perf version 6.8",
        perf_event_paranoid=None,
        kptr_restrict=None,
    )

    def fake_run(command: list[str], *, timeout: int | None) -> subprocess.CompletedProcess[str]:
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

    result = collect_linux_profile(
        ["bench"], tmp_path, capability=capability, effects=LinuxProfilerEffects(run=fake_run)
    )

    assert result.status == "error"
    assert result.report_artifact == str(tmp_path / "perf-report.txt")
    assert LinuxProfilerDiagnostic.PERF_REPORT_UNAVAILABLE in result.diagnostics
    assert LinuxProfilerDiagnostic.COLLECTION_FAILED in result.diagnostics


def test_summarize_empty_directory_and_parse_command(tmp_path: Path) -> None:
    summary = summarize_linux_profile(tmp_path)

    assert summary["metadata"] is None
    assert summary["summary"].startswith("linux perf ok; counters: no perf stat counters parsed")
    assert parse_profile_command("bench --scenario 'spsc queue'") == [
        "bench",
        "--scenario",
        "spsc queue",
    ]


def test_collect_degrades_when_perf_unavailable(tmp_path: Path) -> None:
    capability = LinuxProfilerCapability(
        tool=LinuxProfilerTool.NONE,
        perf_path=None,
        perf_version=None,
        perf_event_paranoid=None,
        kptr_restrict=None,
        diagnostics=(LinuxProfilerDiagnostic.PERF_UNAVAILABLE,),
    )

    result = collect_linux_profile(["bench"], tmp_path, capability=capability)

    assert result.status == "error"
    assert result.stat_artifact is None
    assert result.record_artifact is None
    assert result.metadata == str(tmp_path / "metadata.json")
    assert LinuxProfilerDiagnostic.PERF_UNAVAILABLE in result.diagnostics
