"""Linux procfs CPU accounting for the KV-store candidate process set."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

_CLK = os.sysconf("SC_CLK_TCK")


@dataclass(frozen=True)
class ProcessStat:
    pid: int
    parent_pid: int
    process_group: int
    starttime: int
    cpu_ticks: int

    @property
    def identity(self) -> tuple[int, int]:
        return (self.pid, self.starttime)


def linux_preflight(proc_root: Path = Path("/proc")) -> None:
    if sys.platform != "linux":
        raise RuntimeError("KV-store CPU scoring requires Linux procfs")
    if not (proc_root / "net" / "tcp").is_file() or not (proc_root / "self" / "stat").is_file():
        raise RuntimeError("KV-store CPU scoring requires readable Linux procfs")


def read_process_stat(pid: int, proc_root: Path = Path("/proc")) -> ProcessStat | None:
    try:
        raw = (proc_root / str(pid) / "stat").read_text()
        fields = raw[raw.rindex(")") + 2 :].split()
        return ProcessStat(
            pid=int(pid),
            parent_pid=int(fields[1]),
            process_group=int(fields[2]),
            cpu_ticks=int(fields[11]) + int(fields[12]),
            starttime=int(fields[19]),
        )
    except (OSError, ValueError, IndexError):
        return None


def all_process_stats(proc_root: Path = Path("/proc")) -> dict[int, ProcessStat]:
    stats: dict[int, ProcessStat] = {}
    for path in proc_root.iterdir():
        if path.name.isdigit() and (stat := read_process_stat(int(path.name), proc_root)):
            stats[stat.pid] = stat
    return stats


def listener_pids(port: int, proc_root: Path = Path("/proc")) -> set[int]:
    """Return every process owning a listening socket for ``port``."""
    inodes: set[str] = set()
    for table in (proc_root / "net" / "tcp", proc_root / "net" / "tcp6"):
        try:
            lines = table.read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            # local_address is hex "ADDR:PORT"; st == 0A is LISTEN.
            if (
                len(fields) > 9
                and fields[3] == "0A"
                and int(fields[1].rsplit(":", 1)[1], 16) == port
            ):
                inodes.add(fields[9])
    if not inodes:
        return set()
    pids: set[int] = set()
    for pid_dir in proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            for fd in (pid_dir / "fd").iterdir():
                target = os.readlink(fd)
                if target.startswith("socket:[") and target[8:-1] in inodes:
                    pids.add(int(pid_dir.name))
                    break
        except OSError:
            continue
    return pids


def server_processes(
    port: int,
    process_group: int | None = None,
    proc_root: Path = Path("/proc"),
) -> set[int]:
    stats = all_process_stats(proc_root)
    if process_group is not None:
        return {pid for pid, stat in stats.items() if stat.process_group == process_group}
    roots = listener_pids(port, proc_root)
    selected = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, stat in stats.items():
            if stat.parent_pid in selected and pid not in selected:
                selected.add(pid)
                changed = True
    return selected


def cpu_snapshot(
    port: int,
    process_group: int | None = None,
    proc_root: Path = Path("/proc"),
) -> dict[tuple[int, int], int] | None:
    pids = server_processes(port, process_group, proc_root)
    if not pids:
        return None
    stats = [read_process_stat(pid, proc_root) for pid in sorted(pids)]
    resolved = [stat for stat in stats if stat is not None]
    if len(resolved) != len(pids):
        return None
    return {stat.identity: stat.cpu_ticks for stat in resolved}


def cpu_delta_seconds(
    before: dict[tuple[int, int], int] | None,
    after: dict[tuple[int, int], int] | None,
) -> float | None:
    if before is None or after is None or set(before) != set(after):
        return None
    delta_ticks = sum(after[key] - before[key] for key in before)
    if delta_ticks <= 0:
        return None
    return delta_ticks / _CLK
