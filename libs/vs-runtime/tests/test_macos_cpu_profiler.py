import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Never

from vs_runtime.api.infrastructure import (
    MacOSProfilerCapability,
    MacOSProfilerDiagnostic,
    MacOSProfilerEffects,
    MacOSProfilerTool,
    NativeCpuProfilerKind,
    collect_macos_profile,
    detect_macos_profiler,
    parse_profile_command,
    preflight_native_cpu_profiler,
)


class Result(subprocess.CompletedProcess[str]):
    """``subprocess.run`` result with the fields the profiler reads."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        super().__init__(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeProcess:
    """Deterministic process lifecycle used by sampler collection tests."""

    pid = 123

    def __init__(self, *, wait_times_out: bool = False) -> None:
        self.wait_times_out = wait_times_out
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, *, timeout: int) -> int:
        if self.wait_times_out:
            raise subprocess.TimeoutExpired("benchmark", timeout)
        return 0

    def kill(self) -> None:
        self.killed = True


def test_command_line_tools_shim_falls_back_to_sample() -> None:
    capability = detect_macos_profiler(
        system="Darwin",
        which=lambda name: "/usr/bin/sample" if name == "sample" else None,
        run=lambda *_args, **_kwargs: Result(stdout="/Library/Developer/CommandLineTools\n"),
    )
    assert capability.tool is MacOSProfilerTool.SAMPLE
    assert MacOSProfilerDiagnostic.COMMAND_LINE_TOOLS_ONLY in capability.diagnostics


def test_preflight_reports_sample_as_usable() -> None:
    capability = MacOSProfilerCapability(
        MacOSProfilerTool.SAMPLE, None, None, "/usr/bin/sample", None
    )

    result = preflight_native_cpu_profiler(
        NativeCpuProfilerKind.MACOS, detect_macos=lambda: capability
    )

    assert result.usable
    assert result.selected_tool == "sample"
    assert result.details[-1] == "sample_path=/usr/bin/sample"


def test_missing_time_profiler_template_falls_back_to_sample() -> None:
    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["xcode-select", "-p"]:
            return Result(stdout="/Applications/Xcode.app/Contents/Developer\n")
        return Result(stdout="Activity Monitor\n")

    capability = detect_macos_profiler(
        system="Darwin", which=lambda name: f"/usr/bin/{name}", run=run
    )
    assert capability.tool is MacOSProfilerTool.SAMPLE
    assert MacOSProfilerDiagnostic.TIME_PROFILER_UNAVAILABLE in capability.diagnostics


def test_functional_time_profiler_selects_instruments() -> None:
    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["xcode-select", "-p"]:
            return Result(stdout="/Applications/Xcode.app/Contents/Developer\n")
        if command[-2:] == ["list", "templates"]:
            return Result(stdout="Time Profiler\n")
        return Result(stdout="xctrace version 26.0\n")

    capability = detect_macos_profiler(
        system="Darwin", which=lambda name: f"/usr/bin/{name}", run=run
    )
    assert capability.tool is MacOSProfilerTool.XCTRACE
    assert capability.tool_version == "xctrace version 26.0"


def test_detection_reports_unavailable_tools_after_xcode_select_failure() -> None:
    def fail(*_args: object, **_kwargs: object) -> Never:
        _failure_message = "xcode-select unavailable"
        raise OSError(_failure_message)

    capability = detect_macos_profiler(
        system="Darwin", which=lambda _name: None, run=fail, is_file=lambda _path: False
    )
    assert capability.tool is MacOSProfilerTool.NONE
    assert capability.diagnostics[-2:] == (
        MacOSProfilerDiagnostic.TIME_PROFILER_UNAVAILABLE,
        MacOSProfilerDiagnostic.SAMPLE_UNAVAILABLE,
    )


def test_collection_persists_reproduction_metadata(tmp_path: Path) -> None:
    result = collect_macos_profile(
        ["./benchmark"], tmp_path, capability=detect_macos_profiler(system="Linux")
    )
    metadata = Path(result.metadata).read_text()
    assert result.status == "error"
    assert '"diagnostic_only": true' in metadata
    assert '"scored_benchmark": false' in metadata


def test_instruments_collection_builds_bounded_launch_command(tmp_path: Path) -> None:
    capability = MacOSProfilerCapability(
        MacOSProfilerTool.XCTRACE,
        "/Applications/Xcode.app/Contents/Developer",
        "/usr/bin/xctrace",
        "/usr/bin/sample",
        "xctrace 26",
    )
    calls: list[tuple[Sequence[str], dict[str, object]]] = []

    def run(command: Sequence[str], **kwargs: object) -> Result:
        calls.append((command, kwargs))
        return Result()

    result = collect_macos_profile(
        ["./benchmark", "--workers", "2"],
        tmp_path,
        duration=7,
        capability=capability,
        effects=MacOSProfilerEffects(run=run, now=lambda: 100.0),
    )

    assert result.status == "ok"
    assert result.artifact == str(tmp_path / "time-profile.trace")
    assert result.command == (
        "/usr/bin/xctrace",
        "record",
        "--template",
        "Time Profiler",
        "--time-limit",
        "7s",
        "--output",
        str(tmp_path / "time-profile.trace"),
        "--launch",
        "--",
        "./benchmark",
        "--workers",
        "2",
    )
    assert calls[0][1]["timeout"] == 37


def test_collection_converts_profiler_launch_error_to_diagnostic(tmp_path: Path) -> None:
    capability = MacOSProfilerCapability(
        MacOSProfilerTool.XCTRACE, None, "/usr/bin/xctrace", None, None
    )

    failure = OSError("cannot execute")

    def fail_run(*_args: object, **_kwargs: object) -> Never:
        raise failure

    result = collect_macos_profile(
        ["./benchmark"],
        tmp_path,
        capability=capability,
        effects=MacOSProfilerEffects(run=fail_run),
    )
    assert result.status == "error"
    assert MacOSProfilerDiagnostic.COLLECTION_FAILED in result.diagnostics
    assert "cannot execute" in Path(result.metadata).read_text()


def test_permission_failure_and_child_target_are_structured(tmp_path: Path) -> None:
    capability = detect_macos_profiler(
        system="Darwin",
        which=lambda name: "/usr/bin/sample" if name == "sample" else None,
        run=lambda *_args, **_kwargs: Result(stdout="/Library/Developer/CommandLineTools\n"),
    )
    process = FakeProcess()

    def run(command: Sequence[str], **_kwargs: object) -> Result:
        if command[:2] == ["ps", "-axo"]:
            return Result(stdout="123 1\n456 123\nmalformed\n")
        return Result(1, stderr="Operation not permitted")

    result = collect_macos_profile(
        ["./benchmark"],
        tmp_path,
        capability=capability,
        effects=MacOSProfilerEffects(
            run=run,
            start=lambda *_args, **_kwargs: process,
            sleep=lambda _seconds: None,
        ),
    )
    assert result.target_pid == 456
    assert MacOSProfilerDiagnostic.ATTACH_DENIED in result.diagnostics


def test_sample_kills_launcher_when_graceful_wait_times_out(tmp_path: Path) -> None:
    capability = MacOSProfilerCapability(
        MacOSProfilerTool.SAMPLE, None, None, "/usr/bin/sample", None
    )
    process = FakeProcess(wait_times_out=True)

    def run(command: Sequence[str], **_kwargs: object) -> Result:
        return Result(stdout="123 1\n") if command[:2] == ["ps", "-axo"] else Result()

    result = collect_macos_profile(
        ["./benchmark"],
        tmp_path,
        capability=capability,
        effects=MacOSProfilerEffects(
            run=run,
            start=lambda *_args, **_kwargs: process,
            sleep=lambda _seconds: None,
        ),
    )
    assert result.status == "ok"
    assert result.target_pid == 123
    assert process.killed


def test_parse_command_preserves_quoted_arguments_without_shell_execution() -> None:
    assert parse_profile_command('python bench.py --label "queue run"') == [
        "python",
        "bench.py",
        "--label",
        "queue run",
    ]
