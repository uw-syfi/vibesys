"""Differential fuzz gate (differential-dataflow `bfs`) — the anti-memorization
oracle: the candidate must match the pristine round-0 engine on **many inputs it
was never told about**, not just the two fixed workloads.

`equivalence_gate.py` checks output-equivalence on the two workloads in
`reference/workload.py`. That is enough to catch an honest bug, but it is a fixed,
knowable target: an engine could in principle special-case those two inputs (return
a stored answer, short-circuit the known graph) and still pass. The diff-discipline
gate is what currently makes that pointless — but once the architecture gate is
relaxed, memorization/special-casing becomes a live reward hack. This gate closes
that hole mechanically, the way fuzzing always does: it generates a broad set of
*fresh* inputs and requires the candidate to reproduce the pristine engine's output
on **every one of them**. You cannot memorize an input distribution you are not
shown, so the only way to pass is to actually compute BFS.

Two input sources, both graded the same way (candidate vs pristine, live):
  * a curated **corpus** of boundary/regression inputs (a single node, no edges,
    sparse/dense graphs, one-big-batch vs many-small-rounds) — always checked, and
    the place to append a regression seed when a divergence is ever found; and
  * a **fixed-seed random** batch (`--seed`/`--count`) covering the interior of the
    input space. The seed is fixed by default so the gate is deterministic and
    reproducible — a blocking gate in a git-checkpoint loop must give the same
    verdict on re-run. The genuinely-random, seed-varying heavy campaign is a
    separate out-of-band tier (cargo-fuzz), not this per-round gate.

Inputs are input-space, not code-space: they survive an engine rearchitecting, so
this gate keeps working after the diff-discipline gate is dropped.

Exit 0 = PASS (candidate matches pristine on every corpus + random input);
1 = a real output divergence on some input (a correctness / memorization defect);
2 = a build/setup error (the gate could not render a verdict).

Usage (the Judge runs this alongside the other gates):
  python3 accuracy_checker/differential_fuzz_gate.py \
      --engine-cmd 'engine/target/release/examples/bfs' \
      --rebuild-cmd 'cargo build --release --example bfs -p differential-dataflow \
                     --offline --manifest-path engine/Cargo.toml'
"""

from __future__ import annotations

import argparse
import os
import random
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

_RUN_TIMEOUT_S = 300
_DEFAULT_SEED = 20260816
_DEFAULT_COUNT = 24

# Curated boundary/regression corpus (all validated to run clean at -w 1). Append a
# failing input here as a regression seed whenever a divergence is ever found.
_CORPUS = [
    ["1", "0", "1", "1"],  # single node, no edges (degenerate boundary)
    ["50", "100", "10", "3"],  # tiny graph
    ["500", "200", "5", "4"],  # sparse (fewer edges than nodes → disconnected)
    ["1000", "5000", "50", "5"],  # medium
    ["2000", "16000", "200", "10"],  # denser
    ["300", "1500", "1", "15"],  # many small rounds (batch 1)
    ["300", "1500", "300", "1"],  # one big batch, single round
    ["3000", "20000", "100", "8"],  # largest fuzz input
]


def _rand_workload(rng):
    """A valid random bfs base-argv (nodes, edges, batch, rounds), sized so each run
    is fast (<~0.3s) — the goal is input *breadth*, not per-input scale."""
    nodes = rng.randint(20, 3000)
    edges = rng.randint(0, min(nodes * 8, 20000))
    batch = rng.randint(1, 300)
    rounds = rng.randint(1, 12)
    return [str(nodes), str(edges), str(batch), str(rounds)]


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=_RUN_TIMEOUT_S)


def _build_pristine():
    """Build the pristine `_ref_engine/` bfs binary once; reuse if already present."""
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


def _run_engine(binary, base):
    """Run one bfs invocation (base argv + `inspect -w 1`); return (ok, norm_or_err)."""
    args = [*base, "inspect", "-w", "1"]
    try:
        p = _run([binary, *args])
    except subprocess.TimeoutExpired:
        return False, f"timed out after {_RUN_TIMEOUT_S}s"
    if p.returncode != 0:
        return False, f"exit {p.returncode}: {(p.stderr or p.stdout).strip()[:200]}"
    return True, workload.normalize(p.stdout)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--engine-cmd",
        required=True,
        help="candidate bfs binary (no workload args; the gate appends them)",
    )
    ap.add_argument("--rebuild-cmd", default=None, help="shell command to rebuild the candidate")
    ap.add_argument("--seed", type=int, default=_DEFAULT_SEED, help="fixed RNG seed (reproducible)")
    ap.add_argument("--count", type=int, default=_DEFAULT_COUNT, help="number of random inputs")
    args = ap.parse_args()

    candidate_bin = shlex.split(args.engine_cmd)
    if len(candidate_bin) != 1:
        print(
            "differential-fuzz-gate ERROR: --engine-cmd must be just the binary path.",
            file=sys.stderr,
        )
        return 2
    candidate_bin = candidate_bin[0]

    rng = random.Random(args.seed)
    inputs = [("corpus", w) for w in _CORPUS]
    inputs += [("random", _rand_workload(rng)) for _ in range(args.count)]

    print("differential fuzz gate — candidate must match the pristine engine on every input")
    print(f"  candidate : {candidate_bin}")
    print(f"  pristine  : {_PRISTINE_BIN}")
    print(f"  inputs    : {len(_CORPUS)} corpus + {args.count} random (seed {args.seed})")
    print("-" * 72)

    if args.rebuild_cmd:
        rb = subprocess.run(args.rebuild_cmd, shell=True, capture_output=True, text=True)
        if rb.returncode != 0:
            print(f"candidate REBUILD FAILED: {(rb.stderr or rb.stdout).strip()[:400]}")
            return 2
    if not os.path.exists(candidate_bin):
        print(f"differential-fuzz: SETUP-ERROR — candidate binary not found: {candidate_bin}")
        return 2
    ok, msg = _build_pristine()
    if not ok:
        print(f"differential-fuzz: SETUP-ERROR — pristine build error: {msg}")
        return 2

    mismatches = 0
    checked = 0
    for kind, base in inputs:
        label = f"[{kind}] {' '.join(base)}"
        gold_ok, gold = _run_engine(_PRISTINE_BIN, base)
        if not gold_ok:
            # The reference itself could not run this input → our generator's bug, not
            # the candidate's. Fail as setup so it is fixed rather than blamed on the engine.
            print(f"  {label:<44}  SETUP-ERROR (pristine): {gold}")
            return 2
        cand_ok, cand = _run_engine(candidate_bin, base)
        checked += 1
        if not cand_ok:
            print(f"  {label:<44}  FAIL (candidate did not run): {cand}")
            mismatches += 1
            continue
        if cand != gold:
            gl, cl = gold.splitlines(), cand.splitlines()
            ndiff = sum(1 for a, b in zip(gl, cl, strict=False) if a != b) + abs(len(gl) - len(cl))
            print(
                f"  {label:<44}  FAIL (diverges: pristine={len(gl)} lines, "
                f"candidate={len(cl)} lines, ~{ndiff} differing)"
            )
            mismatches += 1

    print("-" * 72)
    if mismatches == 0:
        print(
            f"DIFFERENTIAL-FUZZ: PASS — candidate matched the pristine engine on all "
            f"{checked} inputs (memorizing a fixed workload cannot pass this)."
        )
        return 0
    print(
        f"DIFFERENTIAL-FUZZ: FAIL — candidate diverged from the pristine engine on "
        f"{mismatches}/{checked} inputs. The engine must actually compute BFS on every "
        f"input, not special-case the fixed workloads."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
