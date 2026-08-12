"""Equivalence gate (differential-dataflow `bfs`) — the candidate must produce
**byte-identical BFS output** to the pristine round-0 upstream engine.

This is the correctness contract for the in-place superoptimization loop. The
agent may only micro-optimize the vendored differential-dataflow source
(`engine/`); any edit that changes what the engine *computes* must be caught. It
is caught here, generically, with no per-query Python truth to reimplement:

  1. Build the **pristine** round-0 engine (`_ref_engine/`, framework-managed,
     never edited) once and cache the binary. Run it LIVE on every fixed
     workload (`reference/workload.py`) → normalized golden output.
  2. Build the **candidate** engine (`engine/`) via `--rebuild-cmd` and run its
     binary on the same workloads → normalized candidate output.
  3. Require byte-for-byte equality of the normalized output on EVERY workload.

Running the pristine engine live on ≥2 distinct workloads (canonical +
perturbation) is the anti-memorization mechanism: there is no stored golden file
to hardcode, and an engine that special-cases one input still has to reproduce
the real BFS result on the other. `normalize()` (in `workload.py`) keeps only the
consolidated data tuples, sorted — so print-order and timing lines never matter.

Exit 0 = PASS (all workloads equivalent); 1 = a real output mismatch;
2 = a build/setup error (the gate could not render a verdict).

Usage (the Judge runs this before accepting any round):
  python3 acc_checker/equivalence_gate.py \
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

# Pristine round-0 engine the framework vendors beside the editable one.
_PRISTINE_DIR = os.path.join(_WORKSPACE, "_ref_engine")
_PRISTINE_MANIFEST = os.path.join(_PRISTINE_DIR, "Cargo.toml")
_PRISTINE_BIN = os.path.join(_PRISTINE_DIR, workload.BFS_BINARY_RELPATH)

_RUN_TIMEOUT_S = 600


def _run(argv, *, cwd=None):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=_RUN_TIMEOUT_S)


def _build_pristine():
    """Build the pristine `_ref_engine/` bfs binary once; reuse if already present.

    The pristine tree is never edited, so a cached binary is always valid for the
    current run. Returns (ok, message)."""
    if os.path.exists(_PRISTINE_BIN):
        return True, "cached"
    if not os.path.isdir(_PRISTINE_DIR):
        return False, f"pristine engine dir missing: {_PRISTINE_DIR}"
    p = _run(workload.build_cmd(_PRISTINE_MANIFEST))
    if p.returncode != 0:
        return False, (p.stderr or p.stdout).strip()[:400]
    if not os.path.exists(_PRISTINE_BIN):
        return False, "pristine build reported success but produced no bfs binary"
    return True, "built"


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
            "equivalence-gate ERROR: --engine-cmd must be just the binary path (the gate "
            "appends workload args); got multiple tokens.",
            file=sys.stderr,
        )
        return 2
    candidate_bin = candidate_bin[0]

    print("equivalence gate — candidate bfs output must match the pristine round-0 engine")
    print(f"  candidate : {candidate_bin}")
    print(f"  pristine  : {_PRISTINE_BIN}")
    print(f"  workloads : {len(workload.WORKLOADS)} (canonical + perturbation)")
    print("-" * 72)

    if args.rebuild_cmd:
        rb = subprocess.run(args.rebuild_cmd, shell=True, capture_output=True, text=True)
        if rb.returncode != 0:
            print(f"candidate REBUILD FAILED: {(rb.stderr or rb.stdout).strip()[:400]}")
            return 2

    ok, msg = _build_pristine()
    if not ok:
        print(f"pristine engine build error: {msg}")
        return 2

    all_match = True
    for wl in workload.WORKLOADS:
        gold_ok, gold = _run_engine(_PRISTINE_BIN, wl)
        if not gold_ok:
            print(f"  {' '.join(wl):<40}  SETUP-ERROR (pristine): {gold}")
            return 2
        cand_ok, cand = _run_engine(candidate_bin, wl)
        if not cand_ok:
            print(f"  {' '.join(wl):<40}  FAIL (candidate did not run): {cand}")
            all_match = False
            continue
        if cand == gold:
            print(f"  {' '.join(wl):<40}  PASS  ({gold.count(chr(10)) + 1} data lines match)")
        else:
            gl = gold.splitlines()
            cl = cand.splitlines()
            ndiff = sum(1 for a, b in zip(gl, cl, strict=False) if a != b) + abs(len(gl) - len(cl))
            print(
                f"  {' '.join(wl):<40}  FAIL  (output differs: pristine={len(gl)} lines, "
                f"candidate={len(cl)} lines, ~{ndiff} differing)"
            )
            all_match = False

    print("-" * 72)
    if all_match:
        print("EQUIVALENCE: PASS — candidate reproduces the pristine BFS output on all workloads.")
        return 0
    print(
        "EQUIVALENCE: FAIL — the candidate's output diverges from the pristine round-0 engine. "
        "A micro-optimization must not change what the engine computes."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
