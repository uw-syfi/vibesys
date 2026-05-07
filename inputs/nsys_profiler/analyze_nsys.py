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
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_db(path: str) -> tuple[sqlite3.Connection, dict[int, str]]:
    """Open the SQLite file and build the string map."""
    conn = sqlite3.connect(path)
    strings: dict[int, str] = {}
    try:
        strings = dict(conn.execute("SELECT id, value FROM StringIds").fetchall())
    except sqlite3.OperationalError:
        pass
    return conn, strings


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        return any(
            row[1] == column
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        )
    except sqlite3.OperationalError:
        return False


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
    if len(parts) > 2:
        name = "::".join(parts[-2:])
    return name


def _resolve_name(name_val, strings: dict[int, str]) -> str:
    if isinstance(name_val, int) and name_val in strings:
        return _short_kernel_name(strings[name_val])
    elif isinstance(name_val, str):
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
        sqlite_path = p.with_suffix(".sqlite")
        if sqlite_path.exists():
            sqlite_path.unlink()
        subprocess.run(
            ["nsys", "export", "--type=sqlite", f"--output={sqlite_path}", str(p)],
            check=True, capture_output=True, text=True,
        )
        return str(sqlite_path)
    return path


# ---------------------------------------------------------------------------
# Subcommand: export
# ---------------------------------------------------------------------------


def cmd_export(args):
    """Export .nsys-rep to .sqlite."""
    out = _ensure_sqlite(args.report)
    print(f"Exported to: {out}")


# ---------------------------------------------------------------------------
# Subcommand: tables
# ---------------------------------------------------------------------------


def cmd_tables(args):
    """List non-empty tables in the SQLite export."""
    conn, _ = _open_db(_ensure_sqlite(args.report))
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    for (name,) in tables:
        try:
            cnt = conn.execute(f"SELECT COUNT(*) FROM [{name}]").fetchone()[0]
        except sqlite3.OperationalError:
            cnt = "?"
        if cnt and cnt != "?" and int(cnt) > 0:
            print(f"  {name}: {cnt} rows")


# ---------------------------------------------------------------------------
# Subcommand: kernels
# ---------------------------------------------------------------------------


def cmd_kernels(args):
    """Top GPU kernels by total execution time."""
    conn, strings = _open_db(_ensure_sqlite(args.report))
    if not _table_exists(conn, "CUPTI_ACTIVITY_KIND_KERNEL"):
        print("(No kernel data found.)")
        return
    name_col = _kernel_name_col(conn)
    if not name_col:
        print("(No kernel name column found.)")
        return

    rows = conn.execute(
        f"""SELECT {name_col}, COUNT(*), SUM(end-start), AVG(end-start)
            FROM CUPTI_ACTIVITY_KIND_KERNEL
            GROUP BY {name_col} ORDER BY SUM(end-start) DESC LIMIT ?""",
        (args.top,),
    ).fetchall()
    if not rows:
        print("(No kernels recorded.)")
        return

    total_ns = sum(r[2] for r in rows)
    print(f"{'Kernel':<55s} {'Count':>7s} {'Total(us)':>11s} {'Avg(us)':>9s} {'%GPU':>6s}")
    print("-" * 92)
    for name_id, cnt, tot, avg in rows:
        name = _resolve_name(name_id, strings)
        pct = tot / total_ns * 100 if total_ns else 0
        print(f"{name:<55s} {cnt:>7d} {tot/1000:>11.1f} {avg/1000:>9.1f} {pct:>5.1f}%")
    total_launches = sum(r[1] for r in rows)
    print(f"\nTotal GPU kernel time: {total_ns/1e6:.2f} ms")
    print(f"Total kernel launches: {total_launches}")


# ---------------------------------------------------------------------------
# Subcommand: cpu-overhead
# ---------------------------------------------------------------------------


def cmd_cpu_overhead(args):
    """CPU-side CUDA runtime overhead and launch-bound detection."""
    conn, strings = _open_db(_ensure_sqlite(args.report))
    if not _table_exists(conn, "CUPTI_ACTIVITY_KIND_RUNTIME"):
        print("(No CUDA runtime data.)")
        return

    row = conn.execute(
        "SELECT COUNT(*), SUM(end-start) FROM CUPTI_ACTIVITY_KIND_RUNTIME"
    ).fetchone()
    total_calls, total_ns = row if row else (0, 0)
    total_ns = total_ns or 0
    print(f"Total CUDA runtime API calls: {total_calls}")
    print(f"Total CPU time in CUDA APIs:  {total_ns/1e6:.2f} ms")

    has_cbid = _column_exists(conn, "CUPTI_ACTIVITY_KIND_RUNTIME", "cbid")
    has_nameId = _column_exists(conn, "CUPTI_ACTIVITY_KIND_RUNTIME", "nameId")

    if has_nameId:
        api_rows = conn.execute(
            """SELECT s.value, COUNT(*), SUM(r.end-r.start), AVG(r.end-r.start)
               FROM CUPTI_ACTIVITY_KIND_RUNTIME r
               LEFT JOIN StringIds s ON r.nameId = s.id
               GROUP BY r.nameId ORDER BY SUM(r.end-r.start) DESC LIMIT 10"""
        ).fetchall()
        if api_rows:
            print(f"\n{'API Function':<40s} {'Count':>8s} {'Total(us)':>11s} {'Avg(us)':>9s}")
            print("-" * 72)
            for name, cnt, tot, avg in api_rows:
                print(f"{(name or '?'):<40s} {cnt:>8d} {tot/1000:>11.1f} {avg/1000:>9.1f}")

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
        print(f"\nSynchronization stalls: {sync_count} calls, {sync_total/1e6:.2f} ms")

        # Launch overhead ratio
        launch_filter = (
            "r.nameId IN (SELECT id FROM StringIds WHERE "
            "value LIKE 'cudaLaunchKernel%' OR value LIKE 'cudaLaunchKernelExC%')"
        )
    elif has_cbid:
        cbid_names = {
            33: "cudaLaunchKernel", 49: "cudaMemcpyAsync", 59: "cudaMalloc",
            60: "cudaFree", 162: "cudaStreamSynchronize", 163: "cudaDeviceSynchronize",
            164: "cudaEventSynchronize", 211: "cudaLaunchKernelExC",
        }
        api_rows = conn.execute(
            "SELECT cbid, COUNT(*), SUM(end-start), AVG(end-start) "
            "FROM CUPTI_ACTIVITY_KIND_RUNTIME GROUP BY cbid ORDER BY SUM(end-start) DESC LIMIT 10"
        ).fetchall()
        if api_rows:
            print(f"\n{'API Function':<40s} {'Count':>8s} {'Total(us)':>11s} {'Avg(us)':>9s}")
            print("-" * 72)
            for cbid, cnt, tot, avg in api_rows:
                print(f"{cbid_names.get(cbid, f'cbid_{cbid}'):<40s} {cnt:>8d} {tot/1000:>11.1f} {avg/1000:>9.1f}")
        sync_cbids = {162, 163, 164}
        sync_rows = conn.execute(
            f"SELECT cbid, COUNT(*), SUM(end-start) FROM CUPTI_ACTIVITY_KIND_RUNTIME "
            f"WHERE cbid IN ({','.join(str(c) for c in sync_cbids)}) GROUP BY cbid"
        ).fetchall()
        sync_total = sum(r[2] for r in sync_rows) if sync_rows else 0
        sync_count = sum(r[1] for r in sync_rows) if sync_rows else 0
        print(f"\nSynchronization stalls: {sync_count} calls, {sync_total/1e6:.2f} ms")
        launch_filter = "r.cbid IN (33, 211)"
    else:
        return

    if _table_exists(conn, "CUPTI_ACTIVITY_KIND_KERNEL"):
        joined = conn.execute(
            f"""SELECT AVG(r.end-r.start), AVG(k.end-k.start), COUNT(*)
                FROM CUPTI_ACTIVITY_KIND_KERNEL k
                JOIN CUPTI_ACTIVITY_KIND_RUNTIME r ON k.correlationId = r.correlationId
                WHERE {launch_filter}"""
        ).fetchone()
        if joined and joined[2] > 0:
            avg_cpu = joined[0] / 1000
            avg_gpu = joined[1] / 1000
            print(f"\nKernel launch overhead ({joined[2]} matched):")
            print(f"  Avg CPU launch: {avg_cpu:.1f} us")
            print(f"  Avg GPU exec:   {avg_gpu:.1f} us")
            if avg_gpu > 0:
                ratio = avg_cpu / avg_gpu
                print(f"  CPU/GPU ratio:  {ratio:.2f}x")
                if ratio > 1.0:
                    print("  *** LAUNCH-BOUND — CPU slower than GPU ***")


# ---------------------------------------------------------------------------
# Subcommand: idle-gaps
# ---------------------------------------------------------------------------


def cmd_idle_gaps(args):
    """Find largest GPU idle gaps between kernels."""
    conn, strings = _open_db(_ensure_sqlite(args.report))
    if not _table_exists(conn, "CUPTI_ACTIVITY_KIND_KERNEL"):
        print("(No kernel data.)")
        return
    name_col = _kernel_name_col(conn)

    rows = conn.execute(
        f"SELECT {name_col}, start, end, deviceId FROM CUPTI_ACTIVITY_KIND_KERNEL ORDER BY deviceId, start"
    ).fetchall()
    if len(rows) < 2:
        print("(Fewer than 2 kernels.)")
        return

    by_device: dict[int, list] = defaultdict(list)
    for r in rows:
        by_device[r[3]].append(r)

    gaps, total_idle, total_busy = [], 0, 0
    for device_id, kernels in by_device.items():
        intervals = sorted((k[1], k[2]) for k in kernels)
        merged = []
        for s, e in intervals:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        total_busy += sum(e - s for s, e in merged)

        end_map = {k[2]: k for k in kernels}
        start_map = {k[1]: k for k in kernels}
        for i in range(1, len(merged)):
            gap = merged[i][0] - merged[i - 1][1]
            if gap > 1000:
                total_idle += gap
                prev = end_map.get(merged[i-1][1])
                nxt = start_map.get(merged[i][0])
                pn = _resolve_name(prev[0], strings) if prev else "?"
                nn = _resolve_name(nxt[0], strings) if nxt else "?"
                gaps.append((pn, nn, gap))

    gaps.sort(key=lambda x: -x[2])
    total = total_busy + total_idle
    pct = total_idle / total * 100 if total else 0
    print(f"GPU busy: {total_busy/1e6:.2f} ms")
    print(f"GPU idle: {total_idle/1e6:.2f} ms ({pct:.1f}%)")
    print(f"Idle gaps (>1us): {len(gaps)}")
    if gaps[:args.top]:
        print(f"\nTop {min(args.top, len(gaps))} gaps:")
        print(f"  {'Gap(us)':>10s}  {'After':<40s} → {'Before':<40s}")
        print("  " + "-" * 95)
        for pn, nn, g in gaps[:args.top]:
            print(f"  {g/1000:>10.1f}  {pn:<40s} → {nn:<40s}")


# ---------------------------------------------------------------------------
# Subcommand: memory
# ---------------------------------------------------------------------------


def cmd_memory(args):
    """Memory copy and allocation operations."""
    conn, _ = _open_db(_ensure_sqlite(args.report))

    if _table_exists(conn, "CUPTI_ACTIVITY_KIND_MEMCPY"):
        kinds = {1: "HtoD", 2: "DtoH", 3: "HtoA", 4: "AtoH", 5: "AtoA", 8: "DtoD"}
        rows = conn.execute(
            "SELECT copyKind, COUNT(*), SUM(end-start), SUM(bytes) "
            "FROM CUPTI_ACTIVITY_KIND_MEMCPY GROUP BY copyKind ORDER BY SUM(end-start) DESC"
        ).fetchall()
        if rows:
            print(f"{'Dir':<8s} {'Count':>8s} {'Total(us)':>11s} {'Bytes':>14s}")
            print("-" * 45)
            for kind, cnt, tot, byt in rows:
                print(f"{kinds.get(kind, f'k{kind}'):<8s} {cnt:>8d} {tot/1000:>11.1f} {byt:>14,d}")
    else:
        print("(No memcpy data.)")

    if _table_exists(conn, "CUPTI_ACTIVITY_KIND_RUNTIME"):
        has_nameId = _column_exists(conn, "CUPTI_ACTIVITY_KIND_RUNTIME", "nameId")
        if has_nameId:
            rows = conn.execute(
                """SELECT s.value, COUNT(*), SUM(r.end-r.start)
                   FROM CUPTI_ACTIVITY_KIND_RUNTIME r
                   JOIN StringIds s ON r.nameId = s.id
                   WHERE s.value LIKE 'cudaMalloc%' OR s.value LIKE 'cudaFree%'
                   GROUP BY s.value"""
            ).fetchall()
            if rows:
                print(f"\n{'Alloc API':<25s} {'Count':>8s} {'Total(us)':>11s}")
                print("-" * 48)
                for name, cnt, tot in rows:
                    print(f"{name:<25s} {cnt:>8d} {tot/1000:>11.1f}")


# ---------------------------------------------------------------------------
# Subcommand: graph-replays
# ---------------------------------------------------------------------------


def cmd_graph_replays(args):
    """CUDA graph replay statistics from CUPTI_ACTIVITY_KIND_GRAPH_TRACE."""
    conn, strings = _open_db(_ensure_sqlite(args.report))

    if not _table_exists(conn, "CUPTI_ACTIVITY_KIND_GRAPH_TRACE"):
        print("(No graph trace data — CUDA graphs may not be active.)")
        return

    traces = conn.execute(
        "SELECT start, end, graphId, graphExecId FROM CUPTI_ACTIVITY_KIND_GRAPH_TRACE ORDER BY start"
    ).fetchall()
    if not traces:
        print("(Graph trace table is empty.)")
        return

    print(f"Total graph replays: {len(traces)}")

    # Per-graphExecId stats
    by_exec: dict[int, list[int]] = defaultdict(list)
    for s, e, gid, geid in traces:
        by_exec[geid].append(e - s)

    print(f"\n{'GraphExec':>10s} {'Replays':>8s} {'Avg(us)':>10s} {'Min(us)':>10s} {'Max(us)':>10s}")
    print("-" * 52)
    for geid, durs in sorted(by_exec.items()):
        avg = sum(durs) / len(durs) / 1000
        mn = min(durs) / 1000
        mx = max(durs) / 1000
        print(f"{geid:>10d} {len(durs):>8d} {avg:>10.1f} {mn:>10.1f} {mx:>10.1f}")

    # Match with CPU-side cudaGraphLaunch
    if _table_exists(conn, "CUPTI_ACTIVITY_KIND_RUNTIME") and _column_exists(conn, "CUPTI_ACTIVITY_KIND_RUNTIME", "nameId"):
        launch_rows = conn.execute(
            """SELECT r.start, r.end, r.correlationId
               FROM CUPTI_ACTIVITY_KIND_RUNTIME r
               JOIN StringIds s ON r.nameId = s.id
               WHERE s.value LIKE 'cudaGraphLaunch%'
               ORDER BY r.start"""
        ).fetchall()
        if launch_rows:
            cpu_durs = [(r[1] - r[0]) / 1000 for r in launch_rows]
            print(f"\ncudaGraphLaunch calls: {len(cpu_durs)}")
            print(f"  CPU launch avg: {sum(cpu_durs)/len(cpu_durs):.1f} us")
            print(f"  CPU launch min: {min(cpu_durs):.1f} us")
            print(f"  CPU launch max: {max(cpu_durs):.1f} us")

    # Gap between consecutive replays (scheduling overhead)
    if len(traces) >= 2:
        replay_gaps = [traces[i][0] - traces[i-1][1] for i in range(1, len(traces))]
        replay_gaps = [g for g in replay_gaps if g > 0]
        if replay_gaps:
            avg_gap = sum(replay_gaps) / len(replay_gaps) / 1000
            med_gap = sorted(replay_gaps)[len(replay_gaps)//2] / 1000
            print(f"\nGap between replays (scheduling overhead):")
            print(f"  Avg: {avg_gap:.1f} us")
            print(f"  Median: {med_gap:.1f} us")
            print(f"  Min: {min(replay_gaps)/1000:.1f} us")
            print(f"  Max: {max(replay_gaps)/1000:.1f} us")


# ---------------------------------------------------------------------------
# Subcommand: step-timeline
# ---------------------------------------------------------------------------


def cmd_step_timeline(args):
    """Per-decode-step kernel breakdown.

    Detects repeating kernel patterns to identify individual decode steps,
    then shows the kernel mix, GPU time, and inter-kernel gaps for one step.
    Works for eager mode (many kernels per step) — for CUDA graph mode,
    use ``graph-replays`` instead.
    """
    conn, strings = _open_db(_ensure_sqlite(args.report))
    if not _table_exists(conn, "CUPTI_ACTIVITY_KIND_KERNEL"):
        print("(No kernel data.)")
        return
    name_col = _kernel_name_col(conn)
    if not name_col:
        print("(No kernel name column.)")
        return

    # Load steady-state kernels (skip first 60% which is likely warmup/loading)
    total = conn.execute("SELECT COUNT(*) FROM CUPTI_ACTIVITY_KIND_KERNEL").fetchone()[0]
    offset = max(0, int(total * 0.6))
    rows = conn.execute(
        f"SELECT {name_col}, start, end FROM CUPTI_ACTIVITY_KIND_KERNEL ORDER BY start LIMIT 5000 OFFSET ?",
        (offset,),
    ).fetchall()
    if len(rows) < 100:
        print("(Not enough steady-state kernels to detect decode steps.)")
        return

    # Find decode step boundaries: gaps > threshold
    # Adaptive threshold: find the gap that separates intra-step from inter-step
    all_gaps = sorted([rows[i][1] - rows[i-1][2] for i in range(1, len(rows)) if rows[i][1] > rows[i-1][2]], reverse=True)
    if not all_gaps:
        print("(No gaps between kernels.)")
        return

    # Use the largest gap cluster as step boundaries
    # Try thresholds to find one that gives consistent step sizes
    best_thresh = None
    for pct in [0.01, 0.02, 0.05, 0.1]:
        thresh = all_gaps[max(0, int(len(all_gaps) * pct))]
        boundaries = [i for i in range(1, len(rows)) if rows[i][1] - rows[i-1][2] > thresh]
        if len(boundaries) >= 3:
            sizes = [boundaries[j+1] - boundaries[j] for j in range(len(boundaries)-1)]
            # Check consistency: most steps should be similar size
            if sizes and max(sizes) < 3 * min(sizes):
                best_thresh = thresh
                break

    if best_thresh is None:
        # Fallback: use a fixed threshold
        best_thresh = all_gaps[min(5, len(all_gaps)-1)] if len(all_gaps) > 5 else 100000

    boundaries = [i for i in range(1, len(rows)) if rows[i][1] - rows[i-1][2] > best_thresh]
    if len(boundaries) < 2:
        print(f"(Could not detect decode step boundaries. Try graph-replays if CUDA graphs are active.)")
        return

    # Analyze the Nth step (default: 2nd, to skip any warmup artifact)
    step_idx = min(args.step, len(boundaries) - 1)
    start_i = boundaries[step_idx]
    end_i = boundaries[step_idx + 1] if step_idx + 1 < len(boundaries) else len(rows)
    step_rows = rows[start_i:end_i]

    n_kernels = len(step_rows)
    gpu_time = sum(r[2] - r[1] for r in step_rows)
    wall_time = step_rows[-1][2] - step_rows[0][1] if step_rows else 0
    gap_time = sum(max(0, step_rows[i][1] - step_rows[i-1][2]) for i in range(1, len(step_rows)))

    sizes = [boundaries[j+1] - boundaries[j] for j in range(min(5, len(boundaries)-1))]
    print(f"Detected {len(boundaries)} decode steps (threshold: {best_thresh/1000:.0f} us)")
    print(f"Kernels per step: {sizes}")
    print(f"\n=== Decode step {step_idx} ({n_kernels} kernels) ===")
    print(f"GPU time:  {gpu_time/1000:.0f} us")
    print(f"Gap time:  {gap_time/1000:.0f} us")
    print(f"Wall time: {wall_time/1000:.0f} us")
    print(f"GPU util:  {gpu_time/wall_time*100:.0f}%" if wall_time else "")

    # Kernel breakdown
    kstats = defaultdict(lambda: {"count": 0, "total": 0})
    for r in step_rows:
        name = _resolve_name(r[0], strings)
        kstats[name]["count"] += 1
        kstats[name]["total"] += r[2] - r[1]

    print(f"\n{'Kernel':<50s} {'Cnt':>5s} {'Total(us)':>10s} {'Avg(us)':>8s} {'%step':>6s}")
    print("-" * 83)
    for name, s in sorted(kstats.items(), key=lambda x: -x[1]["total"]):
        pct = s["total"] / gpu_time * 100 if gpu_time else 0
        print(f"{name:<50s} {s['count']:>5d} {s['total']/1000:>10.1f} {s['total']/s['count']/1000:>8.1f} {pct:>5.1f}%")

    # Top gap transitions
    gstats = defaultdict(lambda: {"count": 0, "total": 0})
    for i in range(1, len(step_rows)):
        g = step_rows[i][1] - step_rows[i-1][2]
        if g > 0:
            pn = _resolve_name(step_rows[i-1][0], strings)[:20]
            nn = _resolve_name(step_rows[i][0], strings)[:20]
            gstats[f"{pn:20s} → {nn}"]["count"] += 1
            gstats[f"{pn:20s} → {nn}"]["total"] += g

    print(f"\n{'Gap transition':<45s} {'Cnt':>5s} {'Total(us)':>10s} {'Avg(us)':>8s}")
    print("-" * 72)
    for key, s in sorted(gstats.items(), key=lambda x: -x[1]["total"])[:10]:
        print(f"{key:<45s} {s['count']:>5d} {s['total']/1000:>10.1f} {s['total']/s['count']/1000:>8.1f}")
    print(f"\nTotal intra-step gap: {gap_time/1000:.0f} us")


# ---------------------------------------------------------------------------
# Backward-compatible function API (used by tests)
# ---------------------------------------------------------------------------


def _build_string_map(conn: sqlite3.Connection) -> dict[int, str]:
    """Build id → string map from StringIds table (if present)."""
    try:
        return dict(conn.execute("SELECT id, value FROM StringIds").fetchall())
    except sqlite3.OperationalError:
        return {}


def _capture_stdout(fn, *a, **kw) -> str:
    """Run fn() and capture its stdout as a string."""
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a, **kw)
    return buf.getvalue()


def analyze_kernels(conn, strings, top_n=15):
    """Legacy API — returns analysis as a string."""
    class _A:
        report = ":memory:"
        top = top_n
    # Monkey-patch _open_db for this call
    saved = globals().get("_open_db")
    globals()["_open_db"] = lambda p: (conn, strings)
    try:
        return _capture_stdout(cmd_kernels, _A())
    finally:
        globals()["_open_db"] = saved


def analyze_cpu_overhead(conn, strings):
    class _A:
        report = ":memory:"
    saved = globals().get("_open_db")
    globals()["_open_db"] = lambda p: (conn, strings)
    try:
        return _capture_stdout(cmd_cpu_overhead, _A())
    finally:
        globals()["_open_db"] = saved


def analyze_gpu_idle_gaps(conn, strings, top_n=10):
    class _A:
        report = ":memory:"
        top = top_n
    saved = globals().get("_open_db")
    globals()["_open_db"] = lambda p: (conn, strings)
    try:
        return _capture_stdout(cmd_idle_gaps, _A())
    finally:
        globals()["_open_db"] = saved


def analyze_memory_ops(conn):
    class _A:
        report = ":memory:"
    saved = globals().get("_open_db")
    globals()["_open_db"] = lambda p: (conn, {})
    try:
        return _capture_stdout(cmd_memory, _A())
    finally:
        globals()["_open_db"] = saved


# ---------------------------------------------------------------------------
# Subcommand: query
# ---------------------------------------------------------------------------


def cmd_query(args):
    """Run arbitrary SQL against the nsys SQLite export."""
    conn, _ = _open_db(_ensure_sqlite(args.report))
    try:
        cur = conn.execute(args.sql)
        if cur.description:
            headers = [d[0] for d in cur.description]
            print("\t".join(headers))
            for row in cur.fetchall():
                print("\t".join(str(v) for v in row))
        else:
            print("(No results.)")
    except sqlite3.OperationalError as e:
        print(f"SQL error: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand: summary (legacy all-in-one)
# ---------------------------------------------------------------------------


def cmd_summary(args):
    """All-in-one analysis (legacy mode)."""
    args.top = getattr(args, "top", 15)
    args.step = getattr(args, "step", 1)
    print("=" * 70)
    print("  NSYS PROFILE ANALYSIS")
    print("=" * 70)
    print("\n## GPU Kernel Summary\n")
    cmd_kernels(args)
    print("\n## CPU Overhead Analysis\n")
    cmd_cpu_overhead(args)
    print("\n## GPU Idle Gap Analysis\n")
    cmd_idle_gaps(args)
    print("\n## Memory Operations\n")
    cmd_memory(args)
    print("\n## CUDA Graph Replays\n")
    cmd_graph_replays(args)
    print("\n## Decode Step Timeline\n")
    cmd_step_timeline(args)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
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
    p.add_argument("--step", type=int, default=1, help="Which decode step to analyze (0-indexed, default: 1)")

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
