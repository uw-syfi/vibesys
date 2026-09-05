"""Accuracy checker (differential-dataflow `bfs`) — the single correctness gate
the VibeSys harness invokes (`[accuracy] command`).

This is a thin ORCHESTRATOR. It builds the candidate `engine/` once, then runs the
mechanical behavioral gates ported from the vibe-serve bundle, each of which is an
independent module under `accuracy_checker/`:

  1. equivalence      — candidate bfs output is byte-identical to the pristine
                        round-0 engine (`_ref_engine/`, regenerated LIVE) on every
                        fixed workload (canonical + perturbation).
  2. differential-fuzz — candidate matches the pristine engine on a broad set of
                        fresh corpus + fixed-seed random inputs (anti-memorization).
  3. determinism      — metamorphic: identical output across worker counts (-w N).
  4. crash-recovery   — SIGKILL mid-run + restart reproduces the clean-run output.
  5. sanitizer        — build under ThreadSanitizer, run multi-worker, no data race.

It deliberately does NOT implement diff-discipline (the `diff -ru _ref_engine
engine` "is every hunk a micro-opt" judgment) — that stays in the LLM judge prompt,
not this mechanical script.

Exit protocol (mirrors kv-store/accuracy_checker/checker.py):
  * exit 0  — every gate PASSED (all checks passed).
  * exit 1  — at least one gate FAILED a real correctness check.
  * exit 2  — (only with --strict) a gate could not run (SETUP-ERROR) and no gate
              FAILED. By default a SETUP-ERROR (e.g. no nightly toolchain for the
              sanitizer, or a crash could not be induced) is reported but does not
              by itself fail the run, since it is an environment limitation, not a
              correctness defect. A real FAIL always exits 1 regardless of --strict.

Usage:
    uv run python accuracy_checker/checker.py
    uv run python accuracy_checker/checker.py --engine-cmd 'engine/target/release/examples/bfs'
    uv run python accuracy_checker/checker.py --gates equivalence,determinism
    uv run python accuracy_checker/checker.py --strict
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKSPACE = os.path.normpath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_WORKSPACE, "reference"))
import workload  # noqa: E402  (reference/workload.py — the single source of truth)

# Editable candidate engine materialized by the "engine" workspace source.
_ENGINE_DIR = os.path.join(_WORKSPACE, "engine")
_ENGINE_MANIFEST = os.path.join(_ENGINE_DIR, "Cargo.toml")
_DEFAULT_ENGINE_BIN = os.path.join(_ENGINE_DIR, workload.BFS_BINARY_RELPATH)

# Gate name -> (script filename, uses_engine_cmd). The sanitizer takes a manifest
# path instead of an engine binary (it builds its own instrumented binary).
_GATES: dict[str, tuple[str, bool]] = {
    "equivalence": ("equivalence_gate.py", True),
    "differential-fuzz": ("differential_fuzz_gate.py", True),
    "determinism": ("determinism_gate.py", True),
    "crash-recovery": ("crash_recovery_gate.py", True),
    "sanitizer": ("sanitizer_gate.py", False),
}
_DEFAULT_ORDER = [
    "equivalence",
    "differential-fuzz",
    "determinism",
    "crash-recovery",
    "sanitizer",
]

# Gate exit-code convention (shared by every ported gate):
_PASS, _FAIL, _SETUP = 0, 1, 2


def _build_candidate() -> tuple[bool, str]:
    """Build the candidate `engine/` bfs binary once, offline. (ok, message)."""
    if not os.path.isfile(_ENGINE_MANIFEST):
        return False, f"engine manifest missing: {_ENGINE_MANIFEST}"
    b = subprocess.run(workload.build_cmd(_ENGINE_MANIFEST), capture_output=True, text=True)
    if b.returncode != 0:
        return False, (b.stderr or b.stdout).strip()[-800:]
    if not os.path.exists(_DEFAULT_ENGINE_BIN):
        return False, "build reported success but produced no bfs binary"
    return True, "built"


def _run_gate(name: str, engine_bin: str) -> int:
    """Run one gate as a subprocess; stream its output; return its exit code."""
    script, uses_engine = _GATES[name]
    path = os.path.join(_HERE, script)
    argv = [sys.executable, path]
    if uses_engine:
        argv += ["--engine-cmd", engine_bin]
    else:  # sanitizer builds its own binary from the manifest
        argv += ["--manifest-path", _ENGINE_MANIFEST]
    print(f"\n{'=' * 72}\n=== GATE: {name}\n{'=' * 72}", flush=True)
    proc = subprocess.run(argv)
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="differential-dataflow bfs accuracy checker")
    ap.add_argument(
        "--engine-cmd",
        default=_DEFAULT_ENGINE_BIN,
        help="candidate bfs binary path (single token; gates append workload args)",
    )
    ap.add_argument(
        "--gates",
        default=",".join(_DEFAULT_ORDER),
        help="comma-separated subset of gates to run (default: all)",
    )
    ap.add_argument(
        "--no-build",
        action="store_true",
        help="do not rebuild engine/ first (assume the binary is already built)",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="treat a gate SETUP-ERROR (could-not-run) as a failure (exit 2)",
    )
    args = ap.parse_args()

    selected = [g.strip() for g in args.gates.split(",") if g.strip()]
    unknown = [g for g in selected if g not in _GATES]
    if unknown:
        print(f"checker ERROR: unknown gate(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"  known gates: {', '.join(_DEFAULT_ORDER)}", file=sys.stderr)
        return 2

    print("differential-dataflow bfs accuracy checker")
    print(f"  engine   : {args.engine_cmd}")
    print(f"  gates    : {', '.join(selected)}")

    if not args.no_build:
        print("  building candidate engine/ (offline)...")
        ok, msg = _build_candidate()
        if not ok:
            print(f"checker FAIL: candidate engine build failed: {msg}")
            return 1
        print(f"  build    : {msg}")

    results: dict[str, int] = {}
    for name in selected:
        results[name] = _run_gate(name, args.engine_cmd)

    print(f"\n{'=' * 72}\nSUMMARY\n{'=' * 72}")
    label = {_PASS: "PASS", _FAIL: "FAIL", _SETUP: "SETUP-ERROR"}
    any_fail = False
    any_setup = False
    for name in selected:
        rc = results[name]
        tag = label.get(rc, f"UNKNOWN({rc})")
        print(f"  {name:<20} {tag}")
        if rc == _PASS:
            continue
        if rc == _SETUP:
            any_setup = True
        else:
            any_fail = True

    if any_fail:
        print("\nACCURACY CHECK FAILED — a gate reported a real correctness defect.")
        return 1
    if any_setup:
        if args.strict:
            print("\nACCURACY CHECK INCOMPLETE (--strict) — a gate could not run (SETUP-ERROR).")
            return 2
        print(
            "\nALL RUNNABLE CHECKS PASSED — one or more gates were skipped as SETUP-ERROR "
            "(environment limitation, not a correctness defect; use --strict to fail on these)."
        )
        return 0
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
