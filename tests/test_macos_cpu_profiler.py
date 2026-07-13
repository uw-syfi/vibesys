from pathlib import Path
from unittest.mock import patch

from vibe_serve.macos_cpu_profiler import (
    DiagnosticCode,
    MacOSProfilerTool,
    collect,
    detect_capability,
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


def test_collection_persists_reproduction_metadata(tmp_path: Path):
    result = collect(["./benchmark"], tmp_path, capability=detect_capability(system="Linux"))
    metadata = Path(result.metadata).read_text()
    assert result.status == "error"
    assert '"diagnostic_only": true' in metadata
    assert '"scored_benchmark": false' in metadata


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
        patch("vibe_serve.macos_cpu_profiler.subprocess.Popen", return_value=process),
        patch("vibe_serve.macos_cpu_profiler._descendants", return_value=[456]),
        patch("vibe_serve.macos_cpu_profiler.time.sleep"),
        patch(
            "vibe_serve.macos_cpu_profiler.subprocess.run",
            return_value=Result(1, stderr="Operation not permitted"),
        ),
    ):
        result = collect(["./benchmark"], tmp_path, capability=capability)
    assert result.target_pid == 456
    assert DiagnosticCode.ATTACH_DENIED in result.diagnostics
