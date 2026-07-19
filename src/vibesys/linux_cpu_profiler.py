"""Capability detection and collection for native Linux CPU profiling."""

from __future__ import annotations

import csv
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


class LinuxProfilerTool(StrEnum):
    PERF = "perf"
    NONE = "none"


class DiagnosticCode(StrEnum):
    NOT_LINUX = "not_linux"
    PERF_UNAVAILABLE = "perf_unavailable"
    PERF_STAT_UNAVAILABLE = "perf_stat_unavailable"
    PERF_RECORD_UNAVAILABLE = "perf_record_unavailable"
    PERF_REPORT_UNAVAILABLE = "perf_report_unavailable"
    PERF_EVENT_PARANOID_RESTRICTIVE = "perf_event_paranoid_restrictive"
    KERNEL_SYMBOLS_RESTRICTED = "kernel_symbols_restricted"
    COLLECTION_FAILED = "collection_failed"


@dataclass(frozen=True)
class Capability:
    tool: LinuxProfilerTool
    perf_path: str | None
    perf_version: str | None
    perf_event_paranoid: int | None
    kptr_restrict: int | None
    diagnostics: tuple[DiagnosticCode, ...] = ()


@dataclass(frozen=True)
class CollectionResult:
    status: str
    tool: LinuxProfilerTool
    output_dir: str
    stat_artifact: str | None
    record_artifact: str | None
    report_artifact: str | None
    metadata: str
    command: tuple[str, ...]
    diagnostics: tuple[DiagnosticCode, ...]
    counters: tuple[dict[str, str], ...]
    hot_symbols: tuple[str, ...]
    summary: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def detect_capability(
    *,
    system: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Runner = subprocess.run,
) -> Capability:
    """Report whether Linux ``perf`` can be invoked for diagnostic profiling."""

    if (system or platform.system()) != "Linux":
        return Capability(
            LinuxProfilerTool.NONE,
            None,
            None,
            None,
            None,
            (DiagnosticCode.NOT_LINUX,),
        )

    perf_path = which("perf")
    perf_event_paranoid = _read_int(Path("/proc/sys/kernel/perf_event_paranoid"))
    kptr_restrict = _read_int(Path("/proc/sys/kernel/kptr_restrict"))
    diagnostics: list[DiagnosticCode] = []

    if perf_event_paranoid is not None and perf_event_paranoid > 2:
        diagnostics.append(DiagnosticCode.PERF_EVENT_PARANOID_RESTRICTIVE)
    if kptr_restrict is not None and kptr_restrict > 0:
        diagnostics.append(DiagnosticCode.KERNEL_SYMBOLS_RESTRICTED)

    if not perf_path:
        diagnostics.append(DiagnosticCode.PERF_UNAVAILABLE)
        return Capability(
            LinuxProfilerTool.NONE,
            None,
            None,
            perf_event_paranoid,
            kptr_restrict,
            tuple(diagnostics),
        )

    try:
        version = run([perf_path, "--version"], capture_output=True, text=True, timeout=5)
        perf_version = (version.stdout or version.stderr).strip() or None
    except (OSError, subprocess.SubprocessError):
        diagnostics.append(DiagnosticCode.PERF_UNAVAILABLE)
        return Capability(
            LinuxProfilerTool.NONE,
            perf_path,
            None,
            perf_event_paranoid,
            kptr_restrict,
            tuple(diagnostics),
        )

    try:
        stat = run(
            [perf_path, "stat", "-e", "cycles", "--", "true"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if stat.returncode != 0:
            diagnostics.append(DiagnosticCode.PERF_STAT_UNAVAILABLE)
    except (OSError, subprocess.SubprocessError):
        diagnostics.append(DiagnosticCode.PERF_STAT_UNAVAILABLE)

    return Capability(
        LinuxProfilerTool.PERF,
        perf_path,
        perf_version,
        perf_event_paranoid,
        kptr_restrict,
        tuple(diagnostics),
    )


def _run_text(
    command: list[str],
    *,
    timeout: int | None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def _parse_perf_stat_csv(path: Path) -> tuple[dict[str, str], ...]:
    if not path.is_file():
        return ()
    counters: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.reader(fh):
            if len(row) < 3:
                continue
            value, unit, event = (item.strip() for item in row[:3])
            if not event or value.startswith("#"):
                continue
            counters.append({"event": event, "value": value, "unit": unit})
    return tuple(counters)


def _extract_hot_symbols(report_text: str, *, limit: int = 20) -> tuple[str, ...]:
    hot: list[str] = []
    for line in report_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        first = stripped.split(maxsplit=1)[0]
        if not first.endswith("%"):
            continue
        hot.append(stripped)
        if len(hot) >= limit:
            break
    return tuple(hot)


def collect(
    command: list[str],
    output_dir: Path,
    *,
    capability: Capability | None = None,
    timeout: int | None = None,
    frequency: int = 99,
    call_graph: str = "fp",
) -> CollectionResult:
    """Run separate diagnostic ``perf stat`` and ``perf record`` workloads."""

    capability = capability or detect_capability()
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    diagnostics = list(capability.diagnostics)
    stat_path = output_dir / "perf-stat.csv"
    record_path = output_dir / "perf.data"
    report_path = output_dir / "perf-report.txt"
    executed: dict[str, list[str]] = {}
    stderr: dict[str, str] = {}
    returncodes: dict[str, int | None] = {}

    if capability.tool is not LinuxProfilerTool.PERF or not capability.perf_path:
        metadata_path = _write_metadata(
            output_dir,
            command=command,
            executed=executed,
            capability=capability,
            diagnostics=diagnostics,
            stderr=stderr,
            returncodes=returncodes,
            started=started,
            timeout=timeout,
            frequency=frequency,
            call_graph=call_graph,
        )
        return CollectionResult(
            "error",
            capability.tool,
            str(output_dir),
            None,
            None,
            None,
            str(metadata_path),
            tuple(command),
            tuple(diagnostics),
            (),
            (),
            "Linux perf is unavailable; see metadata diagnostics.",
        )

    stat_command = [
        capability.perf_path,
        "stat",
        "-x",
        ",",
        "-o",
        str(stat_path),
        "-e",
        "cycles,instructions,branches,branch-misses,cache-misses,context-switches,cpu-migrations",
        "--",
        *command,
    ]
    record_command = [
        capability.perf_path,
        "record",
        "-F",
        str(frequency),
        "-g",
        "--call-graph",
        call_graph,
        "-o",
        str(record_path),
        "--",
        *command,
    ]
    report_command = [
        capability.perf_path,
        "report",
        "--stdio",
        "--no-children",
        "--sort",
        "comm,dso,symbol",
        "--percent-limit",
        "0.5",
        "-i",
        str(record_path),
    ]

    for name, profiler_command in (("stat", stat_command), ("record", record_command)):
        executed[name] = profiler_command
        try:
            result = _run_text(profiler_command, timeout=timeout)
            returncodes[name] = result.returncode
            stderr[name] = result.stderr[-4000:]
            if result.returncode != 0:
                diagnostics.append(
                    DiagnosticCode.PERF_STAT_UNAVAILABLE
                    if name == "stat"
                    else DiagnosticCode.PERF_RECORD_UNAVAILABLE
                )
        except (OSError, subprocess.SubprocessError) as exc:
            returncodes[name] = None
            stderr[name] = str(exc)
            diagnostics.append(
                DiagnosticCode.PERF_STAT_UNAVAILABLE
                if name == "stat"
                else DiagnosticCode.PERF_RECORD_UNAVAILABLE
            )

    report_text = ""
    if record_path.is_file():
        executed["report"] = report_command
        try:
            report = _run_text(report_command, timeout=60)
            returncodes["report"] = report.returncode
            stderr["report"] = report.stderr[-4000:]
            report_text = report.stdout
            report_path.write_text(report_text, encoding="utf-8")
            if report.returncode != 0:
                diagnostics.append(DiagnosticCode.PERF_REPORT_UNAVAILABLE)
        except (OSError, subprocess.SubprocessError) as exc:
            returncodes["report"] = None
            stderr["report"] = str(exc)
            diagnostics.append(DiagnosticCode.PERF_REPORT_UNAVAILABLE)

    counters = _parse_perf_stat_csv(stat_path)
    hot_symbols = _extract_hot_symbols(report_text)
    if any(
        code.name.startswith("PERF_") and code is not DiagnosticCode.PERF_EVENT_PARANOID_RESTRICTIVE
        for code in diagnostics
    ):
        diagnostics.append(DiagnosticCode.COLLECTION_FAILED)

    metadata_path = _write_metadata(
        output_dir,
        command=command,
        executed=executed,
        capability=capability,
        diagnostics=diagnostics,
        stderr=stderr,
        returncodes=returncodes,
        started=started,
        timeout=timeout,
        frequency=frequency,
        call_graph=call_graph,
    )
    status = (
        "ok"
        if not any(code is DiagnosticCode.COLLECTION_FAILED for code in diagnostics)
        else "error"
    )
    summary = _format_summary(status, counters, hot_symbols, diagnostics, output_dir)
    return CollectionResult(
        status,
        capability.tool,
        str(output_dir),
        str(stat_path) if stat_path.exists() else None,
        str(record_path) if record_path.exists() else None,
        str(report_path) if report_path.exists() else None,
        str(metadata_path),
        tuple(command),
        tuple(diagnostics),
        counters,
        hot_symbols,
        summary,
    )


def _write_metadata(
    output_dir: Path,
    *,
    command: list[str],
    executed: dict[str, list[str]],
    capability: Capability,
    diagnostics: list[DiagnosticCode],
    stderr: dict[str, str],
    returncodes: dict[str, int | None],
    started: float,
    timeout: int | None,
    frequency: int,
    call_graph: str,
) -> Path:
    metadata_path = output_dir / "metadata.json"
    metadata = {
        "schema_version": 1,
        "diagnostic_only": True,
        "scored_benchmark": False,
        "tool": capability.tool,
        "command": command,
        "profiler_commands": executed,
        "returncodes": returncodes,
        "timeout_seconds": timeout,
        "frequency_hz": frequency,
        "call_graph": call_graph,
        "os": platform.platform(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "symbol_tools": {name: shutil.which(name) for name in ("addr2line", "nm", "objdump")},
        "started_at_epoch": started,
        "diagnostics": diagnostics,
        "capability": asdict(capability),
        "stderr": stderr,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, default=str) + "\n")
    return metadata_path


def _format_summary(
    status: str,
    counters: tuple[dict[str, str], ...],
    hot_symbols: tuple[str, ...],
    diagnostics: list[DiagnosticCode],
    output_dir: Path,
) -> str:
    counter_text = ", ".join(
        f"{counter['event']}={counter['value']}{counter['unit']}" for counter in counters[:8]
    )
    if not counter_text:
        counter_text = "no perf stat counters parsed"
    symbol_text = "; ".join(hot_symbols[:5]) if hot_symbols else "no hot symbols parsed"
    diagnostic_text = ", ".join(code.value for code in diagnostics) or "none"
    return (
        f"linux perf {status}; counters: {counter_text}; hot symbols: "
        f"{symbol_text}; diagnostics: {diagnostic_text}; artifacts: {output_dir}"
    )


def summarize(output_dir: Path) -> dict:
    """Read persisted artifacts and return a bounded machine-readable summary."""

    metadata_path = output_dir / "metadata.json"
    report_path = output_dir / "perf-report.txt"
    metadata = (
        json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    )
    report_text = (
        report_path.read_text(encoding="utf-8", errors="replace") if report_path.exists() else ""
    )
    counters = _parse_perf_stat_csv(output_dir / "perf-stat.csv")
    hot_symbols = _extract_hot_symbols(report_text)
    diagnostics = tuple(DiagnosticCode(item) for item in metadata.get("diagnostics", []))
    return {
        "metadata": str(metadata_path) if metadata_path.exists() else None,
        "stat_artifact": str(output_dir / "perf-stat.csv")
        if (output_dir / "perf-stat.csv").exists()
        else None,
        "record_artifact": str(output_dir / "perf.data")
        if (output_dir / "perf.data").exists()
        else None,
        "report_artifact": str(report_path) if report_path.exists() else None,
        "counters": list(counters),
        "hot_symbols": list(hot_symbols),
        "diagnostics": [item.value for item in diagnostics],
        "summary": _format_summary(
            "ok" if DiagnosticCode.COLLECTION_FAILED not in diagnostics else "error",
            counters,
            hot_symbols,
            list(diagnostics),
            output_dir,
        ),
    }


def parse_command(command: str) -> list[str]:
    """Parse an agent-supplied command without invoking a shell."""

    return shlex.split(command)
