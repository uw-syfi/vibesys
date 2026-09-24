#!/usr/bin/env python3
"""Nsys profile analysis toolkit — subcommand-based.

Each subcommand queries one aspect of an nsys SQLite export.
The agent picks which analyses to run and in what order.

Usage:
    python analyze_nsys.py export profile.nsys-rep        # Export to SQLite
    python analyze_nsys.py tables profile.sqlite           # List available tables
    python analyze_nsys.py kernels profile.sqlite          # Top GPU kernels
    python analyze_nsys.py cpu-overhead profile.sqlite     # CPU launch overhead
    python analyze_nsys.py idle-gaps profile.sqlite        # GPU idle gaps
    python analyze_nsys.py memory profile.sqlite           # Memory ops
    python analyze_nsys.py graph-replays profile.sqlite    # CUDA graph replay stats
    python analyze_nsys.py step-timeline profile.sqlite    # Per-decode-step breakdown
    python analyze_nsys.py query profile.sqlite "SQL"      # Run arbitrary SQL
    python analyze_nsys.py summary profile.sqlite          # All-in-one (legacy)
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import io
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from collections.abc import Callable

_MIN_KERNEL_NAME_COMPONENTS = 2
_MIN_IDLE_GAP_KERNELS = 2
_MIN_GRAPH_REPLAY_TRACES = 2
_MIN_STEP_KERNELS = 100
_MIN_STEP_BOUNDARIES = 3
_MIN_DECODE_STEPS = 2
_GAP_WARNING_NS = 1000
_FALLBACK_GAP_SAMPLE_INDEX = 5
_MIN_GAP_SAMPLE_COUNT = 5
_FALLBACK_GAP_THRESHOLD_NS = 100_000
_NANOSECONDS_PER_MICROSECOND = 1000
_MAX_STEP_SIZE_RATIO = 3


def _print(
    *values: object,
    sep: str = " ",
    end: str = "\n",
    file: TextIO | None = None,
    flush: bool = False,
) -> None:
    """Print user-facing command-line output."""
    if file is None:
        # lint-waiver: LW-008047 [T201]; This standalone CLI intentionally writes user-facing results to stdout.
        print(*values, sep=sep, end=end, flush=flush)  # noqa: T201
    else:
        print(*values, sep=sep, end=end, file=file, flush=flush)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_db(path: str) -> tuple[sqlite3.Connection, dict[int, str]]:
    """Open the SQLite file and build the string map."""
    conn = sqlite3.connect(path)
    strings: dict[int, str] = {}
    with contextlib.suppress(sqlite3.OperationalError):
        strings = dict(conn.execute("SELECT id, value FROM StringIds").fetchall())
    return conn, strings


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        return any(
            row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        )
    except sqlite3.OperationalError:
        return False


def _quote_identifier(identifier: str) -> str:
    """Quote a SQLite identifier that cannot be passed as a bound value."""
    return '"' + identifier.replace('"', '""') + '"'


def _short_kernel_name(raw: str) -> str:
    """Shorten mangled CUDA kernel names for readability."""
    result, depth = [], 0
    for ch in raw:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif depth == 0:
            result.append(ch)
    name = "".join(result).strip()
    parts = name.split("::")
    if len(parts) > _MIN_KERNEL_NAME_COMPONENTS:
        name = "::".join(parts[-2:])
    return name


def _resolve_name(name_val: int | str, strings: dict[int, str]) -> str:
    if isinstance(name_val, int) and name_val in strings:
        return _short_kernel_name(strings[name_val])
    if isinstance(name_val, str):
        return _short_kernel_name(name_val)
    return str(name_val)


def _kernel_name_col(conn: sqlite3.Connection) -> str | None:
    for col in ("shortName", "demangledName"):
        if _column_exists(conn, "CUPTI_ACTIVITY_KIND_KERNEL", col):
            return col
    return None


def _ensure_sqlite(path: str) -> str:
    """If path is .nsys-rep, export to .sqlite and return the sqlite path."""
    p = Path(path)
    if p.suffix == ".nsys-rep":
        nsys = shutil.which("nsys")
        if nsys is None:
            raise FileNotFoundError(errno.ENOENT, "nsys executable was not found on PATH", "nsys")
        sqlite_path = p.with_suffix(".sqlite")
        if sqlite_path.exists():
            sqlite_path.unlink()
        # lint-waiver: LW-008041 [S603]; The resolved NSYS executable receives the fixed export subcommand and path arguments without a shell.
        subprocess.run(  # noqa: S603
            [nsys, "export", "--type=sqlite", f"--output={sqlite_path}", str(p)],
            check=True,
            capture_output=True,
            text=True,
        )
        return str(sqlite_path)
    return path


def _display_api_rows(
    api_rows: list[tuple[object, int, float, float]],
    name_for_id: dict[int, str] | None = None,
) -> None:
    if not api_rows:
        return
    _print(f"\n{'API Function':<40s} {'Count':>8s} {'Total(us)':>11s} {'Avg(us)':>9s}")
    _print("-" * 72)
    for identifier, count, total, average in api_rows:
        name = (
            name_for_id.get(identifier, f"cbid_{identifier}")
            if name_for_id
            else (identifier or "?")
        )
        _print(f"{name:<40s} {count:>8d} {total / 1000:>11.1f} {average / 1000:>9.1f}")


def _device_gaps(
    kernels: list[tuple[int, int, int, int]], strings: dict[int, str]
) -> tuple[list[tuple[str, str, int]], int, int]:
    intervals = sorted((kernel[1], kernel[2]) for kernel in kernels)
    merged: list[tuple[int, int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    total_busy = sum(end - start for start, end in merged)
    end_map = {kernel[2]: kernel for kernel in kernels}
    start_map = {kernel[1]: kernel for kernel in kernels}
    gaps = []
    total_idle = 0
    for index in range(1, len(merged)):
        gap = merged[index][0] - merged[index - 1][1]
        if gap <= _GAP_WARNING_NS:
            continue
        total_idle += gap
        previous = end_map.get(merged[index - 1][1])
        following = start_map.get(merged[index][0])
        before = _resolve_name(previous[0], strings) if previous else "?"
        after = _resolve_name(following[0], strings) if following else "?"
        gaps.append((before, after, gap))
    return gaps, total_busy, total_idle


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def cmd_export(args: argparse.Namespace) -> None:
    """Export .nsys-rep to .sqlite."""
    out = _ensure_sqlite(args.report)
    _print(f"Exported to: {out}")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def cmd_tables(args: argparse.Namespace) -> None:
    """List non-empty tables in the SQLite export."""
    conn, _ = _open_db(_ensure_sqlite(args.report))
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    for (name,) in tables:
        try:
            # lint-waiver: LW-008029 [S608]; SQLite table names come from sqlite_master and must be quoted as identifiers because they cannot be bound as values.
            cnt = conn.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(name)}"  # noqa: S608
            ).fetchone()[0]
        except sqlite3.OperationalError:
            cnt = "?"
        if cnt and cnt != "?" and int(cnt) > 0:
            _print(f"  {name}: {cnt} rows")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def cmd_kernels(args: argparse.Namespace) -> None:
    """Top GPU kernels by total execution time."""
    conn, strings = _open_db(_ensure_sqlite(args.report))
    if not _table_exists(conn, "CUPTI_ACTIVITY_KIND_KERNEL"):
        _print("(No kernel data found.)")
        return
    name_col = _kernel_name_col(conn)
    if not name_col:
        _print("(No kernel name column found.)")
        return

    quoted_name_col = _quote_identifier(name_col)
    # lint-waiver: LW-008030 [S608]; The selected column is restricted to the two known NSYS kernel name columns and must be quoted as an identifier.
    rows = conn.execute(
        f"""SELECT {quoted_name_col}, COUNT(*), SUM(end-start), AVG(end-start)
            FROM CUPTI_ACTIVITY_KIND_KERNEL
            GROUP BY {quoted_name_col} ORDER BY SUM(end-start) DESC LIMIT ?""",  # noqa: S608
        (args.top,),
    ).fetchall()
    if not rows:
        _print("(No kernels recorded.)")
        return

    total_ns = sum(r[2] for r in rows)
    _print(f"{'Kernel':<55s} {'Count':>7s} {'Total(us)':>11s} {'Avg(us)':>9s} {'%GPU':>6s}")
    _print("-" * 92)
    for name_id, cnt, tot, avg in rows:
        name = _resolve_name(name_id, strings)
        pct = tot / total_ns * 100 if total_ns else 0
        _print(f"{name:<55s} {cnt:>7d} {tot / 1000:>11.1f} {avg / 1000:>9.1f} {pct:>5.1f}%")
    total_launches = sum(r[1] for r in rows)
    _print(f"\nTotal GPU kernel time: {total_ns / 1e6:.2f} ms")
    _print(f"Total kernel launches: {total_launches}")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def cmd_cpu_overhead(args: argparse.Namespace) -> None:
    """CPU-side CUDA runtime overhead and launch-bound detection."""
    conn, _strings = _open_db(_ensure_sqlite(args.report))
    if not _table_exists(conn, "CUPTI_ACTIVITY_KIND_RUNTIME"):
        _print("(No CUDA runtime data.)")
        return

    row = conn.execute(
        "SELECT COUNT(*), SUM(end-start) FROM CUPTI_ACTIVITY_KIND_RUNTIME"
    ).fetchone()
    total_calls, total_ns = row or (0, 0)
    total_ns = total_ns or 0
    _print(f"Total CUDA runtime API calls: {total_calls}")
    _print(f"Total CPU time in CUDA APIs:  {total_ns / 1e6:.2f} ms")

    has_cbid = _column_exists(conn, "CUPTI_ACTIVITY_KIND_RUNTIME", "cbid")
    has_name_id = _column_exists(conn, "CUPTI_ACTIVITY_KIND_RUNTIME", "nameId")

    if has_name_id:
        api_rows = conn.execute(
            """SELECT s.value, COUNT(*), SUM(r.end-r.start), AVG(r.end-r.start)
               FROM CUPTI_ACTIVITY_KIND_RUNTIME r
               LEFT JOIN StringIds s ON r.nameId = s.id
               GROUP BY r.nameId ORDER BY SUM(r.end-r.start) DESC LIMIT 10"""
        ).fetchall()
        _display_api_rows(api_rows)

        # Sync stalls
        sync_rows = conn.execute(
            """SELECT s.value, COUNT(*), SUM(r.end-r.start)
               FROM CUPTI_ACTIVITY_KIND_RUNTIME r
               JOIN StringIds s ON r.nameId = s.id
               WHERE s.value LIKE 'cudaStreamSynchronize%'
                  OR s.value LIKE 'cudaDeviceSynchronize%'
                  OR s.value LIKE 'cudaEventSynchronize%'
               GROUP BY s.value"""
        ).fetchall()
        sync_total = sum(r[2] for r in sync_rows) if sync_rows else 0
        sync_count = sum(r[1] for r in sync_rows) if sync_rows else 0
        _print(f"\nSynchronization stalls: {sync_count} calls, {sync_total / 1e6:.2f} ms")

        # Launch overhead ratio
        launch_query = """SELECT AVG(r.end-r.start), AVG(k.end-k.start), COUNT(*)
            FROM CUPTI_ACTIVITY_KIND_KERNEL k
            JOIN CUPTI_ACTIVITY_KIND_RUNTIME r ON k.correlationId = r.correlationId
            WHERE r.nameId IN (SELECT id FROM StringIds WHERE
                value LIKE 'cudaLaunchKernel%' OR value LIKE 'cudaLaunchKernelExC%')"""
    elif has_cbid:
        cbid_names = {
            33: "cudaLaunchKernel",
            49: "cudaMemcpyAsync",
            59: "cudaMalloc",
            60: "cudaFree",
            162: "cudaStreamSynchronize",
            163: "cudaDeviceSynchronize",
            164: "cudaEventSynchronize",
            211: "cudaLaunchKernelExC",
        }
        api_rows = conn.execute(
            "SELECT cbid, COUNT(*), SUM(end-start), AVG(end-start) "
            "FROM CUPTI_ACTIVITY_KIND_RUNTIME GROUP BY cbid ORDER BY SUM(end-start) DESC LIMIT 10"
        ).fetchall()
        _display_api_rows(api_rows, cbid_names)
        sync_cbids = (162, 163, 164)
        sync_rows = conn.execute(
            "SELECT cbid, COUNT(*), SUM(end-start) FROM CUPTI_ACTIVITY_KIND_RUNTIME "
            "WHERE cbid IN (?, ?, ?) GROUP BY cbid",
            sync_cbids,
        ).fetchall()
        sync_total = sum(r[2] for r in sync_rows) if sync_rows else 0
        sync_count = sum(r[1] for r in sync_rows) if sync_rows else 0
        _print(f"\nSynchronization stalls: {sync_count} calls, {sync_total / 1e6:.2f} ms")
        launch_query = """SELECT AVG(r.end-r.start), AVG(k.end-k.start), COUNT(*)
            FROM CUPTI_ACTIVITY_KIND_KERNEL k
            JOIN CUPTI_ACTIVITY_KIND_RUNTIME r ON k.correlationId = r.correlationId
            WHERE r.cbid IN (33, 211)"""
    else:
        return

    if _table_exists(conn, "CUPTI_ACTIVITY_KIND_KERNEL"):
        joined = conn.execute(launch_query).fetchone()
        if joined and joined[2] > 0:
            avg_cpu = joined[0] / 1000
            avg_gpu = joined[1] / 1000
            _print(f"\nKernel launch overhead ({joined[2]} matched):")
            _print(f"  Avg CPU launch: {avg_cpu:.1f} us")
            _print(f"  Avg GPU exec:   {avg_gpu:.1f} us")
            if avg_gpu > 0:
                ratio = avg_cpu / avg_gpu
                _print(f"  CPU/GPU ratio:  {ratio:.2f}x")
                if ratio > 1.0:
                    _print("  *** LAUNCH-BOUND — CPU slower than GPU ***")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def cmd_idle_gaps(args: argparse.Namespace) -> None:
    """Find largest GPU idle gaps between kernels."""
    conn, strings = _open_db(_ensure_sqlite(args.report))
    if not _table_exists(conn, "CUPTI_ACTIVITY_KIND_KERNEL"):
        _print("(No kernel data.)")
        return
    name_col = _kernel_name_col(conn)

    quoted_name_col = _quote_identifier(name_col)
    # lint-waiver: LW-008031 [S608]; The selected column is restricted to the two known NSYS kernel name columns and must be quoted as an identifier.
    rows = conn.execute(
        f"SELECT {quoted_name_col}, start, end, deviceId FROM CUPTI_ACTIVITY_KIND_KERNEL ORDER BY deviceId, start"  # noqa: S608
    ).fetchall()
    if len(rows) < _MIN_IDLE_GAP_KERNELS:
        _print("(Fewer than 2 kernels.)")
        return

    by_device: dict[int, list[tuple[int, int, int, int]]] = defaultdict(list)
    for row in rows:
        by_device[row[3]].append(row)

    gaps: list[tuple[str, str, int]] = []
    total_idle = total_busy = 0
    for kernels in by_device.values():
        device_gaps, device_busy, device_idle = _device_gaps(kernels, strings)
        gaps.extend(device_gaps)
        total_busy += device_busy
        total_idle += device_idle

    gaps.sort(key=lambda x: -x[2])
    total = total_busy + total_idle
    pct = total_idle / total * 100 if total else 0
    _print(f"GPU busy: {total_busy / 1e6:.2f} ms")
    _print(f"GPU idle: {total_idle / 1e6:.2f} ms ({pct:.1f}%)")
    _print(f"Idle gaps (>1us): {len(gaps)}")
    if gaps[: args.top]:
        _print(f"\nTop {min(args.top, len(gaps))} gaps:")
        _print(f"  {'Gap(us)':>10s}  {'After':<40s} → {'Before':<40s}")
        _print("  " + "-" * 95)
        for pn, nn, g in gaps[: args.top]:
            _print(f"  {g / 1000:>10.1f}  {pn:<40s} → {nn:<40s}")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def cmd_memory(args: argparse.Namespace) -> None:
    """Memory copy and allocation operations."""
    conn, _ = _open_db(_ensure_sqlite(args.report))

    if _table_exists(conn, "CUPTI_ACTIVITY_KIND_MEMCPY"):
        kinds = {1: "HtoD", 2: "DtoH", 3: "HtoA", 4: "AtoH", 5: "AtoA", 8: "DtoD"}
        rows = conn.execute(
            "SELECT copyKind, COUNT(*), SUM(end-start), SUM(bytes) "
            "FROM CUPTI_ACTIVITY_KIND_MEMCPY GROUP BY copyKind ORDER BY SUM(end-start) DESC"
        ).fetchall()
        if rows:
            _print(f"{'Dir':<8s} {'Count':>8s} {'Total(us)':>11s} {'Bytes':>14s}")
            _print("-" * 45)
            for kind, cnt, tot, byt in rows:
                _print(
                    f"{kinds.get(kind, f'k{kind}'):<8s} {cnt:>8d} {tot / 1000:>11.1f} {byt:>14,d}"
                )
    else:
        _print("(No memcpy data.)")

    if _table_exists(conn, "CUPTI_ACTIVITY_KIND_RUNTIME"):
        has_name_id = _column_exists(conn, "CUPTI_ACTIVITY_KIND_RUNTIME", "nameId")
        if has_name_id:
            rows = conn.execute(
                """SELECT s.value, COUNT(*), SUM(r.end-r.start)
                   FROM CUPTI_ACTIVITY_KIND_RUNTIME r
                   JOIN StringIds s ON r.nameId = s.id
                   WHERE s.value LIKE 'cudaMalloc%' OR s.value LIKE 'cudaFree%'
                   GROUP BY s.value"""
            ).fetchall()
            if rows:
                _print(f"\n{'Alloc API':<25s} {'Count':>8s} {'Total(us)':>11s}")
                _print("-" * 48)
                for name, cnt, tot in rows:
                    _print(f"{name:<25s} {cnt:>8d} {tot / 1000:>11.1f}")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def cmd_graph_replays(args: argparse.Namespace) -> None:
    """CUDA graph replay statistics from CUPTI_ACTIVITY_KIND_GRAPH_TRACE."""
    conn, _strings = _open_db(_ensure_sqlite(args.report))

    if not _table_exists(conn, "CUPTI_ACTIVITY_KIND_GRAPH_TRACE"):
        _print("(No graph trace data — CUDA graphs may not be active.)")
        return

    traces = conn.execute(
        "SELECT start, end, graphId, graphExecId FROM CUPTI_ACTIVITY_KIND_GRAPH_TRACE ORDER BY start"
    ).fetchall()
    if not traces:
        _print("(Graph trace table is empty.)")
        return

    _print(f"Total graph replays: {len(traces)}")

    # Per-graphExecId stats
    by_exec: dict[int, list[int]] = defaultdict(list)
    for s, e, _gid, geid in traces:
        by_exec[geid].append(e - s)

    _print(
        f"\n{'GraphExec':>10s} {'Replays':>8s} {'Avg(us)':>10s} {'Min(us)':>10s} {'Max(us)':>10s}"
    )
    _print("-" * 52)
    for geid, durs in sorted(by_exec.items()):
        avg = sum(durs) / len(durs) / 1000
        mn = min(durs) / 1000
        mx = max(durs) / 1000
        _print(f"{geid:>10d} {len(durs):>8d} {avg:>10.1f} {mn:>10.1f} {mx:>10.1f}")

    # Match with CPU-side cudaGraphLaunch
    if _table_exists(conn, "CUPTI_ACTIVITY_KIND_RUNTIME") and _column_exists(
        conn, "CUPTI_ACTIVITY_KIND_RUNTIME", "nameId"
    ):
        launch_rows = conn.execute(
            """SELECT r.start, r.end, r.correlationId
               FROM CUPTI_ACTIVITY_KIND_RUNTIME r
               JOIN StringIds s ON r.nameId = s.id
               WHERE s.value LIKE 'cudaGraphLaunch%'
               ORDER BY r.start"""
        ).fetchall()
        if launch_rows:
            cpu_durs = [(r[1] - r[0]) / 1000 for r in launch_rows]
            _print(f"\ncudaGraphLaunch calls: {len(cpu_durs)}")
            _print(f"  CPU launch avg: {sum(cpu_durs) / len(cpu_durs):.1f} us")
            _print(f"  CPU launch min: {min(cpu_durs):.1f} us")
            _print(f"  CPU launch max: {max(cpu_durs):.1f} us")

    # Gap between consecutive replays (scheduling overhead)
    if len(traces) >= _MIN_GRAPH_REPLAY_TRACES:
        replay_gaps = [traces[i][0] - traces[i - 1][1] for i in range(1, len(traces))]
        replay_gaps = [g for g in replay_gaps if g > 0]
        if replay_gaps:
            avg_gap = sum(replay_gaps) / len(replay_gaps) / 1000
            med_gap = sorted(replay_gaps)[len(replay_gaps) // 2] / 1000
            _print("\nGap between replays (scheduling overhead):")
            _print(f"  Avg: {avg_gap:.1f} us")
            _print(f"  Median: {med_gap:.1f} us")
            _print(f"  Min: {min(replay_gaps) / 1000:.1f} us")
            _print(f"  Max: {max(replay_gaps) / 1000:.1f} us")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def _find_step_boundaries(rows: list[tuple[int, int, int]]) -> tuple[int, list[int]] | None:
    """Infer decode-step boundaries from inter-kernel gaps."""
    all_gaps = sorted(
        [rows[i][1] - rows[i - 1][2] for i in range(1, len(rows)) if rows[i][1] > rows[i - 1][2]],
        reverse=True,
    )
    if not all_gaps:
        return None

    best_threshold = None
    for percentile in (0.01, 0.02, 0.05, 0.1):
        threshold = all_gaps[max(0, int(len(all_gaps) * percentile))]
        boundaries = [i for i in range(1, len(rows)) if rows[i][1] - rows[i - 1][2] > threshold]
        if len(boundaries) < _MIN_STEP_BOUNDARIES:
            continue
        sizes = [boundaries[j + 1] - boundaries[j] for j in range(len(boundaries) - 1)]
        if sizes and max(sizes) < _MAX_STEP_SIZE_RATIO * min(sizes):
            best_threshold = threshold
            break

    if best_threshold is None:
        best_threshold = (
            all_gaps[min(_FALLBACK_GAP_SAMPLE_INDEX, len(all_gaps) - 1)]
            if len(all_gaps) > _MIN_GAP_SAMPLE_COUNT
            else _FALLBACK_GAP_THRESHOLD_NS
        )
    boundaries = [i for i in range(1, len(rows)) if rows[i][1] - rows[i - 1][2] > best_threshold]
    return best_threshold, boundaries


def _display_step_timeline(
    rows: list[tuple[int, int, int]],
    boundaries: list[int],
    threshold: int,
    strings: dict[int, str],
    requested_step: int,
) -> None:
    """Print timing and kernel breakdown for one detected decode step."""
    step_index = min(requested_step, len(boundaries) - 1)
    start_index = boundaries[step_index]
    end_index = boundaries[step_index + 1] if step_index + 1 < len(boundaries) else len(rows)
    step_rows = rows[start_index:end_index]
    kernel_count = len(step_rows)
    gpu_time = sum(row[2] - row[1] for row in step_rows)
    wall_time = step_rows[-1][2] - step_rows[0][1] if step_rows else 0
    gap_time = sum(max(0, step_rows[i][1] - step_rows[i - 1][2]) for i in range(1, len(step_rows)))
    step_sizes = [boundaries[j + 1] - boundaries[j] for j in range(min(5, len(boundaries) - 1))]
    _print(f"Detected {len(boundaries)} decode steps (threshold: {threshold / 1000:.0f} us)")
    _print(f"Kernels per step: {step_sizes}")
    _print(f"\n=== Decode step {step_index} ({kernel_count} kernels) ===")
    _print(f"GPU time:  {gpu_time / 1000:.0f} us")
    _print(f"Gap time:  {gap_time / 1000:.0f} us")
    _print(f"Wall time: {wall_time / 1000:.0f} us")
    _print(f"GPU util:  {gpu_time / wall_time * 100:.0f}%" if wall_time else "")

    kernel_stats = defaultdict(lambda: {"count": 0, "total": 0})
    for row in step_rows:
        name = _resolve_name(row[0], strings)
        kernel_stats[name]["count"] += 1
        kernel_stats[name]["total"] += row[2] - row[1]

    _print(f"\n{'Kernel':<50s} {'Cnt':>5s} {'Total(us)':>10s} {'Avg(us)':>8s} {'%step':>6s}")
    _print("-" * 83)
    for name, stats in sorted(kernel_stats.items(), key=lambda item: -item[1]["total"]):
        percent = stats["total"] / gpu_time * 100 if gpu_time else 0
        _print(
            f"{name:<50s} {stats['count']:>5d} {stats['total'] / 1000:>10.1f} "
            f"{stats['total'] / stats['count'] / 1000:>8.1f} {percent:>5.1f}%"
        )

    gap_stats = defaultdict(lambda: {"count": 0, "total": 0})
    for index in range(1, len(step_rows)):
        gap = step_rows[index][1] - step_rows[index - 1][2]
        if gap > 0:
            previous = _resolve_name(step_rows[index - 1][0], strings)[:20]
            following = _resolve_name(step_rows[index][0], strings)[:20]
            transition = f"{previous:20s} → {following}"
            gap_stats[transition]["count"] += 1
            gap_stats[transition]["total"] += gap

    _print(f"\n{'Gap transition':<45s} {'Cnt':>5s} {'Total(us)':>10s} {'Avg(us)':>8s}")
    _print("-" * 72)
    for name, stats in sorted(gap_stats.items(), key=lambda item: -item[1]["total"])[:10]:
        _print(
            f"{name:<45s} {stats['count']:>5d} {stats['total'] / 1000:>10.1f} "
            f"{stats['total'] / stats['count'] / 1000:>8.1f}"
        )
    _print(f"\nTotal intra-step gap: {gap_time / 1000:.0f} us")


def cmd_step_timeline(args: argparse.Namespace) -> None:
    """Per-decode-step kernel breakdown.

    Detects repeating kernel patterns to identify individual decode steps,
    then shows the kernel mix, GPU time, and inter-kernel gaps for one step.
    Works for eager mode (many kernels per step) — for CUDA graph mode,
    use ``graph-replays`` instead.
    """
    conn, strings = _open_db(_ensure_sqlite(args.report))
    if not _table_exists(conn, "CUPTI_ACTIVITY_KIND_KERNEL"):
        _print("(No kernel data.)")
        return
    name_col = _kernel_name_col(conn)
    if not name_col:
        _print("(No kernel name column.)")
        return

    total = conn.execute("SELECT COUNT(*) FROM CUPTI_ACTIVITY_KIND_KERNEL").fetchone()[0]
    offset = max(0, int(total * 0.6))
    quoted_name_col = _quote_identifier(name_col)
    # lint-waiver: LW-008032 [S608]; The selected column is restricted to the two known NSYS kernel name columns and must be quoted as an identifier.
    rows = conn.execute(
        f"SELECT {quoted_name_col}, start, end FROM CUPTI_ACTIVITY_KIND_KERNEL ORDER BY start LIMIT 5000 OFFSET ?",  # noqa: S608
        (offset,),
    ).fetchall()
    if len(rows) < _MIN_STEP_KERNELS:
        _print("(Not enough steady-state kernels to detect decode steps.)")
        return

    detection = _find_step_boundaries(rows)
    if detection is None:
        _print("(No gaps between kernels.)")
        return
    threshold, boundaries = detection
    if len(boundaries) < _MIN_DECODE_STEPS:
        _print(
            "(Could not detect decode step boundaries. Try graph-replays if CUDA graphs are active.)"
        )
        return
    _display_step_timeline(rows, boundaries, threshold, strings, args.step)


# ---------------------------------------------------------------------------
# Backward-compatible function API (used by tests)
# ---------------------------------------------------------------------------


def _build_string_map(conn: sqlite3.Connection) -> dict[int, str]:
    """Build id → string map from StringIds table (if present)."""
    try:
        return dict(conn.execute("SELECT id, value FROM StringIds").fetchall())
    except sqlite3.OperationalError:
        return {}


def _capture_stdout(fn: Callable[..., object], *a: object, **kw: object) -> str:
    """Run fn() and capture its stdout as a string."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a, **kw)
    return buf.getvalue()


def analyze_kernels(conn: sqlite3.Connection, strings: dict[int, str], top_n: int = 15) -> str:
    """Legacy API — returns analysis as a string."""

    class _A:
        report = ":memory:"
        top = top_n

    # Monkey-patch _open_db for this call
    saved = globals().get("_open_db")
    globals()["_open_db"] = lambda _: (conn, strings)
    try:
        return _capture_stdout(cmd_kernels, _A())
    finally:
        globals()["_open_db"] = saved


def analyze_cpu_overhead(conn: sqlite3.Connection, strings: dict[int, str]) -> str:
    """Format the CPU launch-overhead analysis."""

    class _A:
        report = ":memory:"

    saved = globals().get("_open_db")
    globals()["_open_db"] = lambda _: (conn, strings)
    try:
        return _capture_stdout(cmd_cpu_overhead, _A())
    finally:
        globals()["_open_db"] = saved


def analyze_gpu_idle_gaps(
    conn: sqlite3.Connection, strings: dict[int, str], top_n: int = 10
) -> str:
    """Format the GPU idle-gap analysis."""

    class _A:
        report = ":memory:"
        top = top_n

    saved = globals().get("_open_db")
    globals()["_open_db"] = lambda _: (conn, strings)
    try:
        return _capture_stdout(cmd_idle_gaps, _A())
    finally:
        globals()["_open_db"] = saved


def analyze_memory_ops(conn: sqlite3.Connection) -> str:
    """Format memory operation analysis."""

    class _A:
        report = ":memory:"

    saved = globals().get("_open_db")
    globals()["_open_db"] = lambda _: (conn, {})
    try:
        return _capture_stdout(cmd_memory, _A())
    finally:
        globals()["_open_db"] = saved


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def cmd_query(args: argparse.Namespace) -> None:
    """Run arbitrary SQL against the nsys SQLite export."""
    conn, _ = _open_db(_ensure_sqlite(args.report))
    try:
        cur = conn.execute(args.sql)
        if cur.description:
            headers = [d[0] for d in cur.description]
            _print("\t".join(headers))
            for row in cur.fetchall():
                _print("\t".join(str(v) for v in row))
        else:
            _print("(No results.)")
    except sqlite3.OperationalError as e:
        _print(f"SQL error: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand: summary (legacy all-in-one)
# ---------------------------------------------------------------------------


def cmd_summary(args: argparse.Namespace) -> None:
    """All-in-one analysis (legacy mode)."""
    args.top = getattr(args, "top", 15)
    args.step = getattr(args, "step", 1)
    _print("=" * 70)
    _print("  NSYS PROFILE ANALYSIS")
    _print("=" * 70)
    _print("\n## GPU Kernel Summary\n")
    cmd_kernels(args)
    _print("\n## CPU Overhead Analysis\n")
    cmd_cpu_overhead(args)
    _print("\n## GPU Idle Gap Analysis\n")
    cmd_idle_gaps(args)
    _print("\n## Memory Operations\n")
    cmd_memory(args)
    _print("\n## CUDA Graph Replays\n")
    cmd_graph_replays(args)
    _print("\n## Decode Step Timeline\n")
    cmd_step_timeline(args)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the command-line entry point."""
    parser = argparse.ArgumentParser(
        description="Nsys profile analysis toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("export", help="Export .nsys-rep to .sqlite")
    p.add_argument("report")

    p = sub.add_parser("tables", help="List non-empty tables")
    p.add_argument("report")

    p = sub.add_parser("kernels", help="Top GPU kernels by time")
    p.add_argument("report")
    p.add_argument("--top", type=int, default=15)

    p = sub.add_parser("cpu-overhead", help="CPU launch overhead analysis")
    p.add_argument("report")

    p = sub.add_parser("idle-gaps", help="GPU idle gap analysis")
    p.add_argument("report")
    p.add_argument("--top", type=int, default=10)

    p = sub.add_parser("memory", help="Memory copy and allocation ops")
    p.add_argument("report")

    p = sub.add_parser("graph-replays", help="CUDA graph replay statistics")
    p.add_argument("report")

    p = sub.add_parser("step-timeline", help="Per-decode-step kernel breakdown")
    p.add_argument("report")
    p.add_argument(
        "--step", type=int, default=1, help="Which decode step to analyze (0-indexed, default: 1)"
    )

    p = sub.add_parser("query", help="Run arbitrary SQL")
    p.add_argument("report")
    p.add_argument("sql")

    p = sub.add_parser("summary", help="All-in-one analysis (legacy)")
    p.add_argument("report")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--step", type=int, default=1)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    {
        "export": cmd_export,
        "tables": cmd_tables,
        "kernels": cmd_kernels,
        "cpu-overhead": cmd_cpu_overhead,
        "idle-gaps": cmd_idle_gaps,
        "memory": cmd_memory,
        "graph-replays": cmd_graph_replays,
        "step-timeline": cmd_step_timeline,
        "query": cmd_query,
        "summary": cmd_summary,
    }[args.command](args)


if __name__ == "__main__":
    main()
