"""Measured CPU attribution for the differential-dataflow `bfs` engine.

This is the *ranking* half of the profile-guided bottleneck walk: it profiles the
**unmodified** candidate `bfs` binary and produces a deterministic, quantified
list of which engine **components** burn the most CPU, so the loop can attack the
hottest one first. It does NOT score the engine — the scored metric stays
`benchmark/benchmark.py`'s getrusage `cpu_reduction_ratio`; callgrind instruction
reads (`Ir`) are used only to *rank what to attack*.

Why callgrind (not perf): `perf` is unavailable on this box (`perf_event_paranoid`,
no sudo), and an in-process profiler would perturb `engine/` (and the diff-gate).
`valgrind --tool=callgrind` profiles the unmodified binary, is deterministic
(`Ir` is counted, not sampled → a reproducible ranking, which a git-checkpoint loop
requires), and its symbols map straight to differential-dataflow source. It is
~20-50x slower than native, so we profile a dedicated, *smaller*
`workload.ATTRIBUTION_WORKLOAD` in a single run (no warmups/median — `Ir` is noise-free).

Attribution model — the subtle part: with `-O`/generics, most of the hot
instructions are *monomorphized* copies of differential-dataflow code that
callgrind files under `/rustc/.../library/...` (e.g. `vec/mod.rs`, `slice/index.rs`).
Attributing by that file would dump the real cost into a meaningless "stdlib"
bucket. Instead, when a row's mangled symbol carries the `differential_dataflow`
crate id, we parse the **defining module path** immediately following it (ignoring
type-parameter paths, which appear as back-refs) and fold the cost onto the owning
component. Rows with no dd marker fall back to file/symbol heuristics
(smallvec / timely / libc). The component vocabulary is fixed, so the ranking the
loop walks is stable across rounds.

Ir undercounts cache/memory-bound cost; if the top-`Ir` component fails to move
`cpu_reduction_ratio`, add a cachegrind D1/LL-miss pass — do not silently trust `Ir`.

Exit 0 = wrote a ranked attribution; 2 = build/setup/tool error (no verdict).

Usage (the profiler role runs this each profile round):
  python3 profiler/attribute_cpu.py \
      --engine-cmd 'engine/target/release/examples/bfs' \
      --output-json logs/attribution.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKSPACE = os.path.normpath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_WORKSPACE, "reference"))
import workload  # noqa: E402  (reference/workload.py — the single source of truth)

# callgrind is ~20-50x slower than native; the attribution workload is sized small
# (workload.ATTRIBUTION_WORKLOAD) so a single run finishes in a few seconds, but be
# generous in case the agent points this at a larger candidate.
_RUN_TIMEOUT_S = 1800

_DD_CRATE = "differential_dataflow"
_DD_SRC_PREFIX = "differential-dataflow/src/"

# libc allocator / mem* leaf functions (rows with no dd marker).
_MALLOC_FUNCS = {
    "malloc",
    "free",
    "calloc",
    "realloc",
    "cfree",
    "_int_malloc",
    "_int_free",
    "_int_realloc",
}


def _dd_component_from_path(rel: str) -> str:
    """Map a `differential-dataflow/src/<rel>` file to its component."""
    if rel.startswith("trace/implementations/"):
        return "trace/implementations"
    if rel.startswith("trace/cursor/"):
        return "trace/cursor"
    if rel.startswith("trace/"):
        return "trace/other"
    if rel == "operators/reduce.rs":
        return "operators/reduce"
    if rel.startswith("operators/"):
        return "operators/other"
    if rel == "consolidation.rs":
        return "consolidation"
    return "differential-dataflow/other"


def _dd_defining_path(func: str) -> list[str]:
    """Parse the leading length-prefixed idents right after the dd crate id.

    In Rust v0 mangling the defining item's module path follows the crate id
    directly (`...21differential_dataflow9operators6reduce7cursors...`), while
    type-parameter paths appear later as back-refs. Reading only the idents
    immediately after the crate id gives the *owning* module, not a type param.
    """
    idx = func.find(_DD_CRATE)
    if idx < 0:
        return []
    pos = idx + len(_DD_CRATE)
    idents: list[str] = []
    for _ in range(6):  # a handful is plenty to disambiguate the component
        m = re.match(r"(\d+)", func[pos:])
        if not m:
            break
        n = int(m.group(1))
        start = pos + len(m.group(1))
        ident = func[start : start + n]
        if not ident:
            break
        idents.append(ident)
        pos = start + n
    return idents


def _dd_component_from_symbol(func: str) -> str:
    path = _dd_defining_path(func)
    if not path:
        return "differential-dataflow/other"
    head = path[0]
    nxt = path[1] if len(path) > 1 else ""
    if head == "operators":
        return "operators/reduce" if nxt == "reduce" else "operators/other"
    if head == "trace":
        if nxt == "implementations":
            return "trace/implementations"
        if nxt == "cursor":
            return "trace/cursor"
        return "trace/other"
    if head == "consolidation":
        return "consolidation"
    return "differential-dataflow/other"


def classify(file: str, func: str) -> str:
    """Assign a (file, mangled-function) row to a fixed-vocabulary component."""
    f = file.strip()
    # dd's own source, however the path is prefixed by callgrind's CWD
    # (`differential-dataflow/src/...`, `engine/differential-dataflow/src/...`, …).
    marker = f.find(_DD_SRC_PREFIX)
    if marker >= 0:
        return _dd_component_from_path(f[marker + len(_DD_SRC_PREFIX) :])
    # Monomorphized / library / registry rows: trust the mangled symbol.
    if _DD_CRATE in func:
        return _dd_component_from_symbol(func)
    if "8smallvec" in func:
        return "smallvec"
    if "6timely" in func:
        return "timely"
    base = os.path.basename(f)
    if base == "malloc.c" or func in _MALLOC_FUNCS:
        return "libc/malloc"
    if f.endswith(".S") or "memcpy" in func or "memmove" in func or "memset" in func:
        return "libc/mem"
    if f.startswith("/rustc/"):
        return "rust-stdlib"
    return "other"


def _readable(func: str, file: str) -> str:
    """A short human label for a hot function (for `top_functions` hints)."""
    if _DD_CRATE in func:
        path = _dd_defining_path(func)
        if path:
            return "::".join(path[:4])
    base = os.path.basename(file.strip())
    if func and not func.startswith("_R"):
        return f"{base}:{func}"
    return base


_ROW_RE = re.compile(r"^\s*([\d,]+)\s*\(\s*([\d.]+)%\)\s+(\S.*?):(\S.*?)\s*$")


def parse_annotate(text: str) -> list[tuple[int, str, str]]:
    """Parse `callgrind_annotate` output into (ir, file, function) rows.

    Only the `file:function` table (rows after the `file:function` header) is
    consumed; the `PROGRAM TOTALS` line and headers are skipped.
    """
    rows: list[tuple[int, str, str]] = []
    in_table = False
    for line in text.splitlines():
        if "file:function" in line:
            in_table = True
            continue
        if not in_table:
            continue
        m = _ROW_RE.match(line)
        if not m:
            continue
        ir = int(m.group(1).replace(",", ""))
        rest = m.group(3) + ":" + m.group(4)
        # strip a trailing object annotation: " [/path/to/bfs]"
        obj = rest.find(" [")
        if obj >= 0:
            rest = rest[:obj]
        # file and mangled function are separated by the last ':' (neither contains ':')
        file, _, func = rest.rpartition(":")
        if not file:
            continue
        rows.append((ir, file, func))
    return rows


def aggregate(rows: list[tuple[int, str, str]]) -> list[dict]:
    """Fold rows into ranked components with ir, pct, and top function hints."""
    total = sum(ir for ir, _, _ in rows) or 1
    by_comp: dict[str, dict] = {}
    for ir, file, func in rows:
        comp = classify(file, func)
        entry = by_comp.setdefault(comp, {"component": comp, "ir": 0, "_funcs": {}})
        entry["ir"] += ir
        label = _readable(func, file)
        entry["_funcs"][label] = entry["_funcs"].get(label, 0) + ir
    ranked = []
    for entry in by_comp.values():
        top = sorted(entry["_funcs"].items(), key=lambda kv: kv[1], reverse=True)[:3]
        ranked.append(
            {
                "component": entry["component"],
                "ir": entry["ir"],
                "pct": round(entry["ir"] / total * 100, 2),
                "top_functions": [name for name, _ in top],
            }
        )
    # Deterministic order: by Ir desc, then component name for ties.
    ranked.sort(key=lambda c: (-c["ir"], c["component"]))
    return ranked


def _run_callgrind(binary: str, args: list[str], out_file: str) -> tuple[bool, str]:
    """Profile `binary args` under callgrind; write raw counts to `out_file`."""
    cmd = [
        "valgrind",
        "--tool=callgrind",
        "--cache-sim=no",
        f"--callgrind-out-file={out_file}",
        binary,
        *args,
    ]
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_RUN_TIMEOUT_S,
        )
    except FileNotFoundError:
        return False, "valgrind not found on PATH"
    except subprocess.TimeoutExpired:
        return False, f"callgrind timed out after {_RUN_TIMEOUT_S}s"
    if p.returncode != 0 or not os.path.exists(out_file):
        return False, (p.stderr or "callgrind produced no output").strip()[:400]
    return True, "ok"


def _annotate(out_file: str) -> tuple[bool, str]:
    try:
        p = subprocess.run(
            ["callgrind_annotate", "--threshold=100", "--auto=no", out_file],
            capture_output=True,
            text=True,
            timeout=_RUN_TIMEOUT_S,
        )
    except FileNotFoundError:
        return False, "callgrind_annotate not found on PATH"
    if p.returncode != 0:
        return False, (p.stderr or p.stdout).strip()[:400]
    return True, p.stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--engine-cmd",
        required=True,
        help="candidate bfs binary path (no workload args; the harness appends them), "
        "e.g. 'engine/target/release/examples/bfs'",
    )
    ap.add_argument(
        "--rebuild-cmd", default=None, help="shell command to rebuild the candidate first"
    )
    ap.add_argument(
        "--output-json", default=None, help="where to write the ranked attribution JSON"
    )
    args = ap.parse_args()

    tokens = shlex.split(args.engine_cmd)
    if len(tokens) != 1:
        print(
            "attribute-cpu ERROR: --engine-cmd must be just the binary path (the harness "
            "appends the fixed attribution workload args).",
            file=sys.stderr,
        )
        return 2
    binary = tokens[0]

    if args.rebuild_cmd:
        rb = subprocess.run(args.rebuild_cmd, shell=True, capture_output=True, text=True)
        if rb.returncode != 0:
            print(
                f"attribute-cpu ERROR: candidate rebuild failed: {(rb.stderr or rb.stdout).strip()[:400]}"
            )
            return 2
    if not os.path.exists(binary):
        print(f"attribute-cpu ERROR: engine binary not found: {binary}", file=sys.stderr)
        return 2

    wl = workload.ATTRIBUTION_WORKLOAD
    print(f"attribute-cpu — callgrind on attribution workload: {' '.join(wl)}")
    tmpdir = tempfile.mkdtemp(prefix="attrib-cpu-")
    try:
        out_file = os.path.join(tmpdir, "callgrind.out")
        ok, msg = _run_callgrind(binary, wl, out_file)
        if not ok:
            print(f"attribute-cpu ERROR: {msg}", file=sys.stderr)
            return 2
        ok, text = _annotate(out_file)
        if not ok:
            print(f"attribute-cpu ERROR: {text}", file=sys.stderr)
            return 2
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    rows = parse_annotate(text)
    if not rows:
        print("attribute-cpu ERROR: parsed no rows from callgrind_annotate output", file=sys.stderr)
        return 2
    components = aggregate(rows)

    result = {
        "version": 1,
        "workload": wl,
        "total_ir": sum(ir for ir, _, _ in rows),
        "components": components,
    }

    print("-" * 72)
    print(f"{'component':<28} {'Ir %':>7}  top function")
    print("-" * 72)
    for c in components:
        top = c["top_functions"][0] if c["top_functions"] else ""
        print(f"{c['component']:<28} {c['pct']:>6}%  {top}")
    print("-" * 72)
    if components:
        print(f"Top bottleneck: {components[0]['component']} ({components[0]['pct']}% of Ir)")

    if args.output_json:
        os.makedirs(os.path.dirname(os.path.abspath(args.output_json)), exist_ok=True)
        with open(args.output_json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"  wrote {args.output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
