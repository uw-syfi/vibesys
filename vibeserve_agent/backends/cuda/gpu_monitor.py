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
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


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
        return self.memory_total_mib - self.memory_used_mib


@dataclass
class ContentionStatus:
    """Snapshot of GPU contention state."""

    is_contended: bool = False
    #: Processes that appeared on the monitored GPU after the baseline.
    new_procs: list[dict] = field(default_factory=list)
    #: Current GPU memory / utilisation.
    gpu: GpuInfo | None = None
    timestamp: str = ""


# ---------------------------------------------------------------------------
# GPU survey & selection
# ---------------------------------------------------------------------------


def query_gpu_info() -> list[GpuInfo]:
    """Query ``nvidia-smi`` for per-GPU memory and utilisation."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    gpus: list[GpuInfo] = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            gpus.append(GpuInfo(
                index=int(parts[0]),
                uuid=parts[1],
                name=parts[2],
                memory_used_mib=int(parts[3]),
                memory_total_mib=int(parts[4]),
                utilization_pct=int(parts[5]),
            ))
        except (ValueError, IndexError):
            continue
    return gpus


def pick_gpu(gpus: list[GpuInfo] | None = None) -> GpuInfo | None:
    """Return the GPU with the most free memory, or *None* if unavailable."""
    if gpus is None:
        gpus = query_gpu_info()
    if not gpus:
        return None
    return max(gpus, key=lambda g: g.memory_free_mib)


# ---------------------------------------------------------------------------
# Per-process query
# ---------------------------------------------------------------------------


def _query_gpu_procs() -> str:
    """Run ``nvidia-smi`` and return CSV of GPU compute processes."""
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_gpu_memory,gpu_uuid",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def _parse_proc_output(raw: str) -> list[dict]:
    """Parse nvidia-smi compute-apps CSV into process dicts."""
    procs: list[dict] = []
    for line in raw.strip().splitlines():
        if not line.strip() or "pid" in line.lower() and "process" in line.lower():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
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
        procs.append({
            "pid": pid,
            "process_name": parts[1],
            "gpu_mem_mib": mem,
            "gpu_uuid": parts[3],
        })
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
    """

    def __init__(
        self,
        log_dir: Path,
        gpu_uuid: str,
        interval: float = 30.0,
    ) -> None:
        self._log_dir = log_dir
        self._gpu_uuid = gpu_uuid
        self._interval = interval

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._status = ContentionStatus()
        self._baseline_pids: set[int] = set()

    # -- public API ----------------------------------------------------------

    def start(self) -> None:
        """Snapshot the baseline and start the monitoring thread."""
        self._baseline_pids = self._current_pids_on_gpu()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="gpu-contention-monitor", daemon=True,
        )
        self._thread.start()

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
            raw = _query_gpu_procs()
            procs = _parse_proc_output(raw)
        except Exception:
            return set()
        return {p["pid"] for p in procs if p["gpu_uuid"] == self._gpu_uuid}

    def _run(self) -> None:
        """Monitor loop executed in the background thread."""
        log_path = self._log_dir / "gpu_contention.jsonl"
        while not self._stop_event.is_set():
            try:
                raw = _query_gpu_procs()
                procs = _parse_proc_output(raw)
                gpu_procs = [p for p in procs if p["gpu_uuid"] == self._gpu_uuid]

                new_procs = [
                    p for p in gpu_procs if p["pid"] not in self._baseline_pids
                ]

                # Also grab current GPU-level stats
                gpus = query_gpu_info()
                gpu_info = next(
                    (g for g in gpus if g.uuid == self._gpu_uuid), None,
                )

                contention = ContentionStatus(
                    is_contended=len(new_procs) > 0,
                    new_procs=new_procs,
                    gpu=gpu_info,
                    timestamp=datetime.now(timezone.utc).isoformat(),
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
                    with open(log_path, "a") as f:
                        f.write(json.dumps({
                            "timestamp": contention.timestamp,
                            "is_contended": True,
                            "gpu_uuid": self._gpu_uuid,
                            "gpu": gpu_dict,
                            "new_procs": new_procs,
                        }) + "\n")
            except Exception:
                pass
            self._stop_event.wait(self._interval)
