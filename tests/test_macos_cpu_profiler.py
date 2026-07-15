from pathlib import Path
from unittest.mock import patch

from vibe_sys.macos_cpu_profiler import (
    Capability,
    DiagnosticCode,
    MacOSProfilerTool,
    _descendants,
    collect,
    detect_capability,
    parse_command,
)


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_command_line_tools_shim_falls_back_to_sample():
    capability = detect_capability(
        system="Darwin",
        which=lambda name: "/usr/bin/sample" if name == "sample" else None,
        run=lambda *_args, **_kwargs: Result(stdout="/Library/Developer/CommandLineTools\n"),
    )
    assert capability.tool is MacOSProfilerTool.SAMPLE
    assert DiagnosticCode.COMMAND_LINE_TOOLS_ONLY in capability.diagnostics


def test_missing_time_profiler_template_falls_back_to_sample():
    def run(command, **_kwargs):
        if command[:2] == ["xcode-select", "-p"]:
            return Result(stdout="/Applications/Xcode.app/Contents/Developer\n")
        return Result(stdout="Activity Monitor\n")

    capability = detect_capability(system="Darwin", which=lambda name: f"/usr/bin/{name}", run=run)
    assert capability.tool is MacOSProfilerTool.SAMPLE
    assert DiagnosticCode.TIME_PROFILER_UNAVAILABLE in capability.diagnostics


def test_functional_time_profiler_selects_instruments():
    def run(command, **_kwargs):
        if command[:2] == ["xcode-select", "-p"]:
            return Result(stdout="/Applications/Xcode.app/Contents/Developer\n")
        if command[-2:] == ["list", "templates"]:
            return Result(stdout="Time Profiler\n")
        return Result(stdout="xctrace version 26.0\n")

    capability = detect_capability(system="Darwin", which=lambda name: f"/usr/bin/{name}", run=run)
    assert capability.tool is MacOSProfilerTool.XCTRACE
    assert capability.tool_version == "xctrace version 26.0"


def test_detection_reports_unavailable_tools_after_xcode_select_failure():
    def fail(*_args, **_kwargs):
        raise OSError("xcode-select unavailable")

    with patch("vibe_sys.macos_cpu_profiler.Path.is_file", return_value=False):
        capability = detect_capability(system="Darwin", which=lambda _name: None, run=fail)
    assert capability.tool is MacOSProfilerTool.NONE
    assert capability.diagnostics[-2:] == (
        DiagnosticCode.TIME_PROFILER_UNAVAILABLE,
        DiagnosticCode.SAMPLE_UNAVAILABLE,
    )


def test_descendants_returns_nested_processes_and_ignores_malformed_rows():
    result = Result(stdout="10 1\n20 10\nmalformed\n30 20\n40 10\n10 30\n")
    assert _descendants(10, run=lambda *_args, **_kwargs: result) == [20, 40, 30]


def test_collection_persists_reproduction_metadata(tmp_path: Path):
    result = collect(["./benchmark"], tmp_path, capability=detect_capability(system="Linux"))
    metadata = Path(result.metadata).read_text()
    assert result.status == "error"
    assert '"diagnostic_only": true' in metadata
    assert '"scored_benchmark": false' in metadata


def test_instruments_collection_builds_bounded_launch_command(tmp_path: Path):
    capability = Capability(
        MacOSProfilerTool.XCTRACE,
        "/Applications/Xcode.app/Contents/Developer",
        "/usr/bin/xctrace",
        "/usr/bin/sample",
        "xctrace 26",
    )
    with patch(
        "vibe_sys.macos_cpu_profiler.subprocess.run",
        return_value=Result(),
    ) as run:
        result = collect(
            ["./benchmark", "--workers", "2"], tmp_path, duration=7, capability=capability
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
    assert run.call_args.kwargs["timeout"] == 37


def test_collection_converts_profiler_launch_error_to_diagnostic(tmp_path: Path):
    capability = Capability(MacOSProfilerTool.XCTRACE, None, "/usr/bin/xctrace", None, None)
    with patch(
        "vibe_sys.macos_cpu_profiler.subprocess.run",
        side_effect=OSError("cannot execute"),
    ):
        result = collect(["./benchmark"], tmp_path, capability=capability)
    assert result.status == "error"
    assert DiagnosticCode.COLLECTION_FAILED in result.diagnostics
    assert "cannot execute" in Path(result.metadata).read_text()


def test_permission_failure_and_child_target_are_structured(tmp_path: Path):
    capability = detect_capability(
        system="Darwin",
        which=lambda name: "/usr/bin/sample" if name == "sample" else None,
        run=lambda *_args, **_kwargs: Result(stdout="/Library/Developer/CommandLineTools\n"),
    )
    process = type(
        "Process",
        (),
        {
            "pid": 123,
            "terminate": lambda self: None,
            "wait": lambda self, timeout: 0,
            "kill": lambda self: None,
        },
    )()
    with (
        patch("vibe_sys.macos_cpu_profiler.subprocess.Popen", return_value=process),
        patch("vibe_sys.macos_cpu_profiler._descendants", return_value=[456]),
        patch("vibe_sys.macos_cpu_profiler.time.sleep"),
        patch(
            "vibe_sys.macos_cpu_profiler.subprocess.run",
            return_value=Result(1, stderr="Operation not permitted"),
        ),
    ):
        result = collect(["./benchmark"], tmp_path, capability=capability)
    assert result.target_pid == 456
    assert DiagnosticCode.ATTACH_DENIED in result.diagnostics


def test_sample_kills_launcher_when_graceful_wait_times_out(tmp_path: Path):
    capability = Capability(MacOSProfilerTool.SAMPLE, None, None, "/usr/bin/sample", None)
    process = type(
        "Process",
        (),
        {
            "pid": 123,
            "terminate": lambda self: None,
            "wait": lambda self, timeout: (_ for _ in ()).throw(
                __import__("subprocess").TimeoutExpired("benchmark", timeout)
            ),
            "kill": lambda self: setattr(self, "killed", True),
            "killed": False,
        },
    )()
    with (
        patch("vibe_sys.macos_cpu_profiler.subprocess.Popen", return_value=process),
        patch("vibe_sys.macos_cpu_profiler._descendants", return_value=[]),
        patch("vibe_sys.macos_cpu_profiler.time.sleep"),
        patch("vibe_sys.macos_cpu_profiler.subprocess.run", return_value=Result()),
    ):
        result = collect(["./benchmark"], tmp_path, capability=capability)
    assert result.status == "ok"
    assert result.target_pid == 123
    assert process.killed


def test_parse_command_preserves_quoted_arguments_without_shell_execution():
    assert parse_command('python bench.py --label "queue run"') == [
        "python",
        "bench.py",
        "--label",
        "queue run",
    ]
