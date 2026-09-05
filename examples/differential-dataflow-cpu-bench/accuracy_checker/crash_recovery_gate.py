"""Crash-recovery gate (differential-dataflow `bfs`) — oracle C, a **fault-injection**
oracle: killing the engine mid-computation and restarting it must converge to the
**same output as a clean, uninterrupted run**.

Where the determinism gate proves the engine is consistent across worker counts and
the sanitizer gate proves it is race-free, this proves the engine is consistent
across a *crash*: a `SIGKILL` at an arbitrary point followed by a restart in the
same working directory must reproduce the clean-run output byte-for-byte. It is the
"exactly-once under restart" guarantee — the one a diff-discipline gate implied for
free (a stateless recompute engine can't corrupt itself) and that must be checked
explicitly once agents are free to rearchitect.

Today's `bfs` does no filesystem I/O and regenerates its whole input from a fixed
RNG seed, so it passes trivially: a restart just recomputes the identical answer.
The gate is the forward-looking backstop for when a candidate introduces persistent
state as an optimization — a spilled/checkpointed trace, an on-disk arrangement, a
memoization cache. Then a crash mid-write can leave corrupt residue in the working
directory that a naive restart trusts, silently producing a wrong result. Because
the crash run and its restart share one working directory, any such residue is
exercised, and any divergence from the clean output is caught here.

This is a self-consistency (metamorphic) property of the candidate alone — it
compares crash+restart output to the candidate's *own* clean output, not to the
pristine engine (equivalence_gate.py already covers candidate-vs-pristine), so it
keeps working after the architecture gate is relaxed.

Exit 0 = PASS (every crash+restart reproduced the clean output);
1 = a recovery divergence (a real crash-consistency defect);
2 = a build/setup error, including "could not induce a real mid-run crash".

Usage (the Judge runs this alongside the other behavioral gates):
  python3 accuracy_checker/crash_recovery_gate.py \
      --engine-cmd 'engine/target/release/examples/bfs' \
      --rebuild-cmd 'cargo build --release --example bfs -p differential-dataflow \
                     --offline --manifest-path engine/Cargo.toml'
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKSPACE = os.path.normpath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_WORKSPACE, "reference"))
import workload  # noqa: E402  (reference/workload.py — the single source of truth)

_RUN_TIMEOUT_S = 600


# The crash workload runs at -w 1 (single process, so SIGKILL is a clean whole-engine
# crash — no orphaned worker threads/processes) and we kill after observing a few
# stdout lines, which the line-buffered engine emits while genuinely mid-computation.
def _with_w1(wl):
    out = list(wl)
    if "-w" in out:
        i = out.index("-w")
        if i + 1 < len(out):
            out[i + 1] = "1"
    else:
        out += ["-w", "1"]
    return out


# Kill after this many stdout lines — each is a different mid-run crash point
# (graph load, first stable frontier, a later incremental round).
_KILL_AFTER_LINES = (1, 2, 4)


def _clean_run(binary, args, cwd):
    """Run to completion in `cwd`; return (ok, normalized_or_error)."""
    try:
        p = subprocess.run(
            [binary, *args], cwd=cwd, capture_output=True, text=True, timeout=_RUN_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return False, f"clean run timed out after {_RUN_TIMEOUT_S}s"
    if p.returncode != 0:
        return False, f"clean run exit {p.returncode}: {(p.stderr or p.stdout).strip()[:200]}"
    return True, workload.normalize(p.stdout)


def _crash_then_restart(binary, args, cwd, kill_after_lines):
    """Start the engine in `cwd`, SIGKILL after `kill_after_lines` stdout lines, then
    restart it clean in the SAME `cwd`. Returns (crashed, ok, normalized_or_error)."""
    proc = subprocess.Popen(
        [binary, *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,  # line-buffered on our side so readline() returns lines as they arrive
    )
    crashed = False
    try:
        for _ in range(kill_after_lines):
            line = proc.stdout.readline()
            if line == "":  # EOF: the engine finished before we could crash it
                break
        else:
            # Consumed the target number of lines without hitting EOF → still running.
            proc.send_signal(signal.SIGKILL)
            crashed = True
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()

    if not crashed:
        # The process completed before we could inject a crash at this point.
        return False, True, "completed-before-crash"

    # Restart in the SAME working directory so any crash-time residue is exercised.
    ok, out = _clean_run(binary, args, cwd)
    return True, ok, out


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
        help="shell command that rebuilds the candidate engine before grading",
    )
    args = ap.parse_args()

    candidate_bin = shlex.split(args.engine_cmd)
    if len(candidate_bin) != 1:
        print(
            "crash-recovery-gate ERROR: --engine-cmd must be just the binary path "
            "(the gate appends workload args); got multiple tokens.",
            file=sys.stderr,
        )
        return 2
    # Runs happen in temp working dirs, so the binary path must be absolute.
    candidate_bin = os.path.abspath(candidate_bin[0])
    wl = _with_w1(workload.METRIC_WORKLOAD)

    print("crash-recovery gate — kill mid-run, restart, require the clean-run output")
    print(f"  candidate  : {candidate_bin}")
    print(f"  workload   : bfs {' '.join(wl)}")
    print(f"  crashes    : SIGKILL after {list(_KILL_AFTER_LINES)} stdout line(s)")
    print("-" * 72)

    if args.rebuild_cmd:
        rb = subprocess.run(args.rebuild_cmd, shell=True, capture_output=True, text=True)
        if rb.returncode != 0:
            print(f"candidate REBUILD FAILED: {(rb.stderr or rb.stdout).strip()[:400]}")
            return 2

    if not os.path.exists(candidate_bin):
        print(f"CRASH-RECOVERY: SETUP-ERROR — binary not found: {candidate_bin}")
        return 2

    # Golden = a clean, uninterrupted run in its own fresh working dir.
    gold_dir = tempfile.mkdtemp(prefix="crashrec-gold-")
    try:
        ok, golden = _clean_run(candidate_bin, wl, gold_dir)
    finally:
        shutil.rmtree(gold_dir, ignore_errors=True)
    if not ok:
        print(f"CRASH-RECOVERY: SETUP-ERROR — clean golden run failed: {golden}")
        return 2
    golden_lines = golden.count("\n") + 1 if golden else 0

    any_real_crash = False
    all_recovered = True
    for k in _KILL_AFTER_LINES:
        work = tempfile.mkdtemp(prefix=f"crashrec-k{k}-")
        try:
            crashed, ok, out = _crash_then_restart(candidate_bin, wl, work, k)
        finally:
            shutil.rmtree(work, ignore_errors=True)

        if not crashed:
            print(f"  kill@{k} line(s)   SKIP  (engine finished before the crash point)")
            continue
        any_real_crash = True
        if not ok:
            print(f"  kill@{k} line(s)   FAIL  (restart did not run cleanly: {out})")
            all_recovered = False
            continue
        if out == golden:
            print(f"  kill@{k} line(s)   PASS  (restart reproduced all {golden_lines} data lines)")
        else:
            gl, cl = golden.splitlines(), out.splitlines()
            ndiff = sum(1 for a, b in zip(gl, cl, strict=False) if a != b) + abs(len(gl) - len(cl))
            print(
                f"  kill@{k} line(s)   FAIL  (restart diverges from clean run: clean={len(gl)} "
                f"lines, recovered={len(cl)} lines, ~{ndiff} differing)"
            )
            all_recovered = False

    print("-" * 72)
    if not any_real_crash:
        print(
            "CRASH-RECOVERY: SETUP-ERROR — could not induce a real mid-run crash (the engine "
            "finished before every kill point); cannot render a recovery verdict."
        )
        return 2
    if all_recovered:
        print("CRASH-RECOVERY: PASS — every crash+restart reproduced the clean-run output.")
        return 0
    print(
        "CRASH-RECOVERY: FAIL — a crash+restart produced output that diverges from a clean run. "
        "A restart must recover to exactly the result the engine computes without a crash "
        "(likely corrupt persistent state trusted on recovery)."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
