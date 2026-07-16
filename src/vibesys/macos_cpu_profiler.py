"""Capability detection and collection for native macOS CPU profiling."""

from __future__ import annotations

import json
import platform
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path


class MacOSProfilerTool(StrEnum):
    XCTRACE = "instruments"
    SAMPLE = "sample"
    NONE = "none"


class DiagnosticCode(StrEnum):
    NOT_MACOS = "not_macos"
    COMMAND_LINE_TOOLS_ONLY = "command_line_tools_only"
    TIME_PROFILER_UNAVAILABLE = "time_profiler_unavailable"
    SAMPLE_UNAVAILABLE = "sample_unavailable"
    ATTACH_DENIED = "attach_denied"
    TARGET_EXITED = "target_exited"
    COLLECTION_FAILED = "collection_failed"
    HARDWARE_COUNTERS_UNAVAILABLE = "hardware_counters_unavailable"


@dataclass(frozen=True)
class Capability:
    tool: MacOSProfilerTool
    xcode_path: str | None
    xctrace_path: str | None
    sample_path: str | None
    tool_version: str | None
    diagnostics: tuple[DiagnosticCode, ...] = ()


@dataclass(frozen=True)
class CollectionResult:
    status: str
    tool: MacOSProfilerTool
    artifact: str | None
    metadata: str
    target_pid: int | None
    command: tuple[str, ...]
    diagnostics: tuple[DiagnosticCode, ...]
    summary: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


def detect_capability(
    *,
    system: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Runner = subprocess.run,
) -> Capability:
    """Select a functional Time Profiler, then ``sample`` as fallback."""
    if (system or platform.system()) != "Darwin":
        return Capability(
            MacOSProfilerTool.NONE, None, None, None, None, (DiagnosticCode.NOT_MACOS,)
        )

    sample_path = which("sample") or (
        "/usr/bin/sample" if Path("/usr/bin/sample").is_file() else None
    )
    diagnostics: list[DiagnosticCode] = [DiagnosticCode.HARDWARE_COUNTERS_UNAVAILABLE]
    xcode_path: str | None = None
    xctrace_path: str | None = None
    version: str | None = None
    try:
        selected = run(["xcode-select", "-p"], capture_output=True, text=True, timeout=5)
        xcode_path = selected.stdout.strip() if selected.returncode == 0 else None
        if not xcode_path or "/CommandLineTools" in xcode_path:
            diagnostics.append(DiagnosticCode.COMMAND_LINE_TOOLS_ONLY)
        else:
            candidate = Path(xcode_path) / "usr" / "bin" / "xctrace"
            xctrace_path = str(candidate) if candidate.is_file() else which("xctrace")
            if xctrace_path:
                templates = run(
                    [xctrace_path, "list", "templates"], capture_output=True, text=True, timeout=15
                )
                if templates.returncode == 0 and "Time Profiler" in templates.stdout:
                    version_result = run(
                        [xctrace_path, "version"], capture_output=True, text=True, timeout=5
                    )
                    version = (version_result.stdout or version_result.stderr).strip() or None
                    return Capability(
                        MacOSProfilerTool.XCTRACE,
                        xcode_path,
                        xctrace_path,
                        sample_path,
                        version,
                        tuple(diagnostics),
                    )
            diagnostics.append(DiagnosticCode.TIME_PROFILER_UNAVAILABLE)
    except (OSError, subprocess.SubprocessError):
        diagnostics.append(DiagnosticCode.TIME_PROFILER_UNAVAILABLE)

    if sample_path:
        return Capability(
            MacOSProfilerTool.SAMPLE,
            xcode_path,
            xctrace_path,
            sample_path,
            None,
            tuple(diagnostics),
        )
    diagnostics.append(DiagnosticCode.SAMPLE_UNAVAILABLE)
    return Capability(
        MacOSProfilerTool.NONE, xcode_path, xctrace_path, None, None, tuple(diagnostics)
    )


def _descendants(root_pid: int, *, run: Runner = subprocess.run) -> list[int]:
    result = run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=5)
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        try:
            pid, ppid = (int(value) for value in line.split())
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    found, pending, seen = [], [root_pid], {root_pid}
    while pending:
        parent = pending.pop(0)
        for child in children.get(parent, []):
            if child not in seen:
                seen.add(child)
                found.append(child)
                pending.append(child)
    return found


def collect(
    command: list[str],
    output_dir: Path,
    *,
    duration: int = 10,
    warmup: float = 1.0,
    capability: Capability | None = None,
) -> CollectionResult:
    """Run a separate diagnostic workload and persist raw data plus metadata."""
    capability = capability or detect_capability()
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    artifact: Path | None = None
    target_pid: int | None = None
    process_topology: list[int] = []
    diagnostics = list(capability.diagnostics)
    executed: list[str] = []

    try:
        if capability.tool is MacOSProfilerTool.XCTRACE:
            artifact = output_dir / "time-profile.trace"
            executed = [
                capability.xctrace_path or "xctrace",
                "record",
                "--template",
                "Time Profiler",
                "--time-limit",
                f"{duration}s",
                "--output",
                str(artifact),
                "--launch",
                "--",
                *command,
            ]
            result = subprocess.run(executed, capture_output=True, text=True, timeout=duration + 30)
        elif capability.tool is MacOSProfilerTool.SAMPLE:
            process = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(warmup)
            descendants = _descendants(process.pid)
            process_topology = [process.pid, *descendants]
            target_pid = descendants[-1] if descendants else process.pid
            artifact = output_dir / "sample.txt"
            executed = [
                capability.sample_path or "/usr/bin/sample",
                str(target_pid),
                str(duration),
                "-file",
                str(artifact),
            ]
            result = subprocess.run(executed, capture_output=True, text=True, timeout=duration + 15)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        else:
            result = subprocess.CompletedProcess([], 1, "", "No macOS profiler is available")
    except (OSError, subprocess.SubprocessError) as exc:
        result = subprocess.CompletedProcess(executed, 1, "", str(exc))

    stderr = result.stderr or ""
    if "not permitted" in stderr.lower() or "permission" in stderr.lower():
        diagnostics.append(DiagnosticCode.ATTACH_DENIED)
    if result.returncode != 0:
        diagnostics.append(DiagnosticCode.COLLECTION_FAILED)

    metadata_path = output_dir / "metadata.json"
    metadata = {
        "schema_version": 1,
        "diagnostic_only": True,
        "scored_benchmark": False,
        "tool": capability.tool,
        "tool_version": capability.tool_version,
        "command": command,
        "profiler_command": executed,
        "duration_seconds": duration,
        "warmup_seconds": warmup,
        "target_pid": target_pid,
        "process_topology": process_topology,
        "os": platform.platform(),
        "os_build": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "symbol_tools": {
            name: shutil.which(name) for name in ("dsymutil", "atos", "nm", "dwarfdump")
        },
        "started_at_epoch": started,
        "diagnostics": diagnostics,
        "capability": asdict(capability),
        "stderr": stderr[-4000:],
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, default=str) + "\n")
    status = "ok" if result.returncode == 0 else "error"
    summary = f"{capability.tool.value} {status}; target pid {target_pid or 'launched by Instruments'}; artifact {artifact}"
    return CollectionResult(
        status,
        capability.tool,
        str(artifact) if artifact else None,
        str(metadata_path),
        target_pid,
        tuple(executed),
        tuple(diagnostics),
        summary,
    )


def parse_command(command: str) -> list[str]:
    """Parse an agent-supplied command without invoking a shell."""
    return shlex.split(command)
