"""Determinism gate (differential-dataflow `bfs`) — a **metamorphic** oracle: the
candidate must compute the **same BFS result regardless of worker count**.

This is the first behavioral-consistency check that does *not* depend on a diff
against pristine source. Where `equivalence_gate.py` proves *what* the engine
computes (byte-identical to round-0), this proves the engine computes it
*deterministically under parallelism* — the property a rearchitected or
re-parallelized engine is most likely to silently break (data races, unordered
reductions, nondeterministic merge). It lets the harness relax the diff-discipline
gate while still catching the class of regression that gate implicitly guarded.

The metamorphic relation, validated against `bfs.rs`:

  * The graph is generated ONLY by worker 0 from a fixed seed (`&[1,2,3,4]`), so
    the *input* is identical no matter how many workers run.
  * The result is `.consolidate()`'d — a canonical multiset — before printing.
  * Therefore `normalize(bfs @ -w N)` must be **byte-identical for every N**; only
    the inter-worker print *order* may differ, and `normalize()` sorts that away.

So for each fixed workload we run the candidate at several worker counts (and
repeat the multi-worker runs, since a race is often flaky) and require every
normalized output to equal the single-worker golden. Any divergence = the engine
is nondeterministic across workers = FAIL. No pristine engine is needed: this is a
self-consistency (metamorphic) property of the candidate alone.

Exit 0 = PASS (deterministic across all worker counts on all workloads);
1 = nondeterminism detected (a real behavioral defect);
2 = a build/setup error (the gate could not render a verdict).

Usage (the Judge runs this alongside the equivalence gate):
  python3 accuracy_checker/determinism_gate.py \
      --engine-cmd 'engine/target/release/examples/bfs' \
      --rebuild-cmd 'cargo build --release --example bfs -p differential-dataflow \
                     --offline --manifest-path engine/Cargo.toml'
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKSPACE = os.path.normpath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_WORKSPACE, "reference"))
import workload  # noqa: E402  (reference/workload.py — the single source of truth)

_RUN_TIMEOUT_S = 600

# Worker counts to cross-check. 1 is the order-deterministic golden; >1 exercises
# the parallel merge/reduce paths where a race would surface. Repeats catch flaky
# races that only fire on some schedules. Kept small so the gate stays cheap.
_WORKER_COUNTS = (1, 2, 4)
_REPEATS_PER_MULTIWORKER = 3


def _run(argv, *, cwd=None):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=_RUN_TIMEOUT_S)


def _with_workers(wl, n):
    """Return a copy of workload argv with the `-w` value forced to `n`.

    The workload shape is `... inspect -w <workers>` (`reference/workload.py`); if
    `-w` is absent for some reason we append it so the override is always applied.
    """
    out = list(wl)
    if "-w" in out:
        i = out.index("-w")
        if i + 1 < len(out):
            out[i + 1] = str(n)
        else:
            out.append(str(n))
    else:
        out += ["-w", str(n)]
    return out


def _run_engine(binary, args):
    """Run one bfs invocation; return (ok, normalized_output_or_error)."""
    if not os.path.exists(binary):
        return False, f"binary not found: {binary}"
    try:
        p = _run([binary, *args])
    except subprocess.TimeoutExpired:
        return False, f"timed out after {_RUN_TIMEOUT_S}s on args {args}"
    if p.returncode != 0:
        return False, f"exit {p.returncode} on args {args}: {(p.stderr or p.stdout).strip()[:200]}"
    return True, workload.normalize(p.stdout)


def _plan(wl):
    """(worker_count, repeat_index) pairs to run for one workload.

    -w 1 runs once (it is the golden and is order-deterministic); each higher
    worker count runs `_REPEATS_PER_MULTIWORKER` times to expose flaky races.
    """
    plan = [(1, 0)]
    for n in _WORKER_COUNTS:
        if n == 1:
            continue
        plan += [(n, r) for r in range(_REPEATS_PER_MULTIWORKER)]
    return plan


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--engine-cmd",
        required=True,
        help="candidate bfs binary (no workload args; the gate appends them), e.g. "
        "'engine/target/release/examples/bfs'",
    )
    ap.add_argument(
        "--rebuild-cmd",
        default=None,
        help="shell command that rebuilds the candidate engine before grading "
        "(recommended, so the graded binary matches the current source)",
    )
    args = ap.parse_args()

    candidate_bin = shlex.split(args.engine_cmd)
    if len(candidate_bin) != 1:
        print(
            "determinism-gate ERROR: --engine-cmd must be just the binary path (the gate "
            "appends workload args); got multiple tokens.",
            file=sys.stderr,
        )
        return 2
    candidate_bin = candidate_bin[0]

    print("determinism gate — candidate bfs output must be identical across worker counts")
    print(f"  candidate     : {candidate_bin}")
    print(
        f"  worker counts : {list(_WORKER_COUNTS)} (multi-worker repeated {_REPEATS_PER_MULTIWORKER}x)"
    )
    print(f"  workloads     : {len(workload.WORKLOADS)} (canonical + perturbation)")
    print("-" * 72)

    if args.rebuild_cmd:
        rb = subprocess.run(args.rebuild_cmd, shell=True, capture_output=True, text=True)
        if rb.returncode != 0:
            print(f"candidate REBUILD FAILED: {(rb.stderr or rb.stdout).strip()[:400]}")
            return 2

    all_deterministic = True
    for wl in workload.WORKLOADS:
        label = " ".join(wl)
        golden = None
        wl_ok = True
        for n, rep in _plan(wl):
            run_ok, out = _run_engine(candidate_bin, _with_workers(wl, n))
            if not run_ok:
                # A run that cannot execute at all is a setup error, not a verdict.
                print(f"  {label:<40}  SETUP-ERROR (-w {n}): {out}")
                return 2
            if golden is None:
                golden = out
                golden_lines = golden.count("\n") + 1 if golden else 0
                continue
            if out != golden:
                gl, cl = golden.splitlines(), out.splitlines()
                ndiff = sum(1 for a, b in zip(gl, cl, strict=False) if a != b) + abs(
                    len(gl) - len(cl)
                )
                print(
                    f"  {label:<40}  FAIL  (-w {n} rep {rep} diverges from -w 1: "
                    f"golden={len(gl)} lines, this={len(cl)} lines, ~{ndiff} differing)"
                )
                all_deterministic = False
                wl_ok = False
                break  # one divergence is enough to condemn this workload
        if wl_ok:
            print(f"  {label:<40}  PASS  ({golden_lines} data lines identical across all -w)")

    print("-" * 72)
    if all_deterministic:
        print(
            "DETERMINISM: PASS — candidate output is identical across worker counts on all workloads."
        )
        return 0
    print(
        "DETERMINISM: FAIL — the candidate produces different output at different worker counts. "
        "A parallel engine must compute the same result regardless of how many workers run "
        "(likely a data race or an unordered reduction/merge)."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
