"""Background GPU contention monitor.

Provides two capabilities:

1. **GPU selection** — :func:`pick_gpu` queries all GPUs and returns the
   index of the least-loaded one (by memory usage).
2. **Runtime monitoring** — :class:`GpuContentionMonitor` watches a
   specific GPU in a daemon thread and logs when new processes appear on
   it after the agent has started.

Contention events are written to ``gpu_contention.jsonl`` in the
experiment's log directory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from vs_sim.api import Clock, OsThreads, SubprocessProbe, SystemClock, Threads

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from vs_sim.api import CommandProbe, Worker
GPU_QUERY_COLUMN_COUNT = 6
GPU_PROCESS_QUERY_COLUMN_COUNT = 4


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class GpuInfo:
    """Snapshot of a single GPU's state."""

    index: int
    uuid: str
    name: str
    memory_used_mib: int
    memory_total_mib: int
    utilization_pct: int

    @property
    def memory_free_mib(self) -> int:
        """Return free device memory in mebibytes."""
        return self.memory_total_mib - self.memory_used_mib


@dataclass
class ContentionStatus:
    """Snapshot of GPU contention state."""

    is_contended: bool = False
    #: Processes that appeared on the monitored GPU after the baseline.
    new_procs: list[dict[str, Any]] = field(default_factory=list)
    #: Current GPU memory / utilisation.
    gpu: GpuInfo | None = None
    timestamp: str = ""


# ---------------------------------------------------------------------------
# GPU survey & selection
# ---------------------------------------------------------------------------


def parse_gpu_info_output(raw: str) -> list[GpuInfo]:
    """Parse ``nvidia-smi --query-gpu`` CSV into one :class:`GpuInfo` per well-formed row."""
    gpus: list[GpuInfo] = []
    for line in raw.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < GPU_QUERY_COLUMN_COUNT:
            continue
        try:
            gpus.append(
                GpuInfo(
                    index=int(parts[0]),
                    uuid=parts[1],
                    name=parts[2],
                    memory_used_mib=int(parts[3]),
                    memory_total_mib=int(parts[4]),
                    utilization_pct=int(parts[5]),
                )
            )
        except (ValueError, IndexError):
            continue
    return gpus


def pick_gpu(gpus: Sequence[GpuInfo]) -> GpuInfo | None:
    """Return the GPU with the most free memory, or *None* if there is none."""
    if not gpus:
        return None
    return max(gpus, key=lambda g: g.memory_free_mib)


# ---------------------------------------------------------------------------
# Telemetry source
# ---------------------------------------------------------------------------


class GpuTelemetry(Protocol):
    """Where GPU state comes from; both reads are best effort and never raise for a missing driver."""

    def gpus(self) -> list[GpuInfo]:
        """Every GPU's memory and utilisation; empty when no GPU or no driver is present."""
        ...

    def compute_processes(self) -> str:
        """``pid, process_name, used_gpu_memory, gpu_uuid`` CSV rows; empty when unavailable."""
        ...


class NvidiaSmiTelemetry:
    """:class:`GpuTelemetry` read by running ``nvidia-smi``."""

    def __init__(
        self,
        executable: str = "nvidia-smi",
        timeout: float = 10.0,
        *,
        probe: CommandProbe | None = None,
    ) -> None:
        """Run *executable* (looked up on ``PATH`` when bare), giving up after *timeout* seconds."""
        self._probe: CommandProbe = probe or SubprocessProbe()
        self._executable = executable
        self._timeout = timeout

    def gpus(self) -> list[GpuInfo]:
        """Query per-GPU memory and utilisation."""
        return parse_gpu_info_output(
            self._query(
                "--query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu",
            )
        )

    def compute_processes(self) -> str:
        """Query the GPU compute processes."""
        return self._query("--query-compute-apps=pid,process_name,used_gpu_memory,gpu_uuid")

    def _query(self, query: str) -> str:
        result = self._probe.run(
            [self._executable, query, "--format=csv,noheader,nounits"],
            timeout_seconds=self._timeout,
        )
        if result is None:
            return ""
        return result.stdout if result.returncode == 0 else ""


# ---------------------------------------------------------------------------
# Per-process query
# ---------------------------------------------------------------------------


def parse_gpu_process_output(raw: str) -> list[dict[str, Any]]:
    """Parse nvidia-smi compute-apps CSV into process dicts."""
    procs: list[dict[str, Any]] = []
    for line in raw.strip().splitlines():
        if not line.strip() or ("pid" in line.lower() and "process" in line.lower()):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < GPU_PROCESS_QUERY_COLUMN_COUNT:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        mem_str = parts[2].split()[0]
        try:
            mem = int(mem_str)
        except ValueError:
            mem = 0
        procs.append(
            {
                "pid": pid,
                "process_name": parts[1],
                "gpu_mem_mib": mem,
                "gpu_uuid": parts[3],
            }
        )
    return procs


# ---------------------------------------------------------------------------
# Background monitor
# ---------------------------------------------------------------------------


class GpuContentionMonitor:
    """Watch a single GPU for new processes appearing after the agent starts.

    On :meth:`start`, the monitor takes a **baseline snapshot** of PIDs
    already on the target GPU.  From then on, any *new* PID that appears
    on that GPU is treated as contention.

    Parameters
    ----------
    log_dir:
        Directory where ``gpu_contention.jsonl`` is written.
    gpu_uuid:
        UUID of the GPU to monitor (from :func:`pick_gpu`).
    interval:
        Seconds between checks (default 30).
    telemetry:
        Where GPU state is read from (``nvidia-smi`` by default).
    threads:
        The thread and event provider the polling loop runs on.
    clock:
        The epoch clock that stamps contention events.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-692101 [PLR0913]; the monitor's three collaborators are keyword-only seams on top of its three settings.
        self,
        log_dir: Path,
        gpu_uuid: str,
        interval: float = 30.0,
        *,
        telemetry: GpuTelemetry | None = None,
        threads: Threads | None = None,
        clock: Clock | None = None,
    ) -> None:
        """Configure monitoring for one GPU UUID and polling interval."""
        self._log_dir = log_dir
        self._gpu_uuid = gpu_uuid
        self._interval = interval
        self._telemetry: GpuTelemetry = telemetry or NvidiaSmiTelemetry()
        self._threads: Threads = threads or OsThreads()
        self._clock: Clock = clock or SystemClock()

        self._stop_event = self._threads.event()
        self._thread: Worker | None = None
        self._lock = self._threads.lock()
        self._status = ContentionStatus()
        self._baseline_pids: set[int] = set()

    # -- public API ----------------------------------------------------------

    def start(self) -> None:
        """Snapshot the baseline and start the monitoring thread."""
        self._baseline_pids = self._current_pids_on_gpu()
        self._stop_event.clear()
        self._thread = self._threads.spawn(self._run, name="gpu-contention-monitor", daemon=True)

    def stop(self) -> None:
        """Signal the thread to stop and wait for it to exit."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def status(self) -> ContentionStatus:
        """Return the most recent contention snapshot (thread-safe)."""
        with self._lock:
            return self._status

    # -- internal ------------------------------------------------------------

    def _current_pids_on_gpu(self) -> set[int]:
        """Return the set of PIDs currently on the monitored GPU."""
        try:
            raw = self._telemetry.compute_processes()
            procs = parse_gpu_process_output(raw)
        except Exception:  # noqa: BLE001  # lint-waiver: LW-009050 [BLE001]; GPU telemetry is best effort, and any driver failure must leave monitoring available.
            return set()
        return {p["pid"] for p in procs if p["gpu_uuid"] == self._gpu_uuid}

    def _run(self) -> None:
        """Monitor loop executed in the background thread."""
        log_path = self._log_dir / "gpu_contention.jsonl"
        while not self._stop_event.is_set():
            try:
                raw = self._telemetry.compute_processes()
                procs = parse_gpu_process_output(raw)
                gpu_procs = [p for p in procs if p["gpu_uuid"] == self._gpu_uuid]

                new_procs = [p for p in gpu_procs if p["pid"] not in self._baseline_pids]

                # Also grab current GPU-level stats
                gpus = self._telemetry.gpus()
                gpu_info = next(
                    (g for g in gpus if g.uuid == self._gpu_uuid),
                    None,
                )

                contention = ContentionStatus(
                    is_contended=len(new_procs) > 0,
                    new_procs=new_procs,
                    gpu=gpu_info,
                    timestamp=datetime.fromtimestamp(self._clock.now(), UTC).isoformat(),
                )
                with self._lock:
                    self._status = contention

                if contention.is_contended:
                    gpu_dict = None
                    if gpu_info:
                        gpu_dict = {
                            "index": gpu_info.index,
                            "memory_used_mib": gpu_info.memory_used_mib,
                            "memory_total_mib": gpu_info.memory_total_mib,
                            "utilization_pct": gpu_info.utilization_pct,
                        }
                    with log_path.open("a") as f:
                        f.write(
                            json.dumps(
                                {
                                    "timestamp": contention.timestamp,
                                    "is_contended": True,
                                    "gpu_uuid": self._gpu_uuid,
                                    "gpu": gpu_dict,
                                    "new_procs": new_procs,
                                }
                            )
                            + "\n"
                        )
            except Exception:  # noqa: BLE001  # lint-waiver: LW-009051 [BLE001]; GPU telemetry is best effort, and any driver failure must not terminate the monitor thread.
                # Telemetry is best effort and must not terminate the monitor.
                self._stop_event.wait(self._interval)
                continue
            self._stop_event.wait(self._interval)
