"""Sanitizer gate (differential-dataflow `bfs`) — build the candidate under
**ThreadSanitizer** and run it multi-worker; any reported data race is a hard FAIL.

The determinism gate (`determinism_gate.py`) catches a race only when it actually
perturbs the *output*. Many races are silently benign-looking on a given schedule
yet are still undefined behavior that a future compiler/CPU can miscompile. TSan is
the mechanical oracle for that class: it instruments memory accesses and reports a
race the moment two threads touch the same location without synchronization, even
if the observed output happened to be correct this run. This is the anti-"passes
today, breaks tomorrow" backstop for an engine the agent is free to re-parallelize.

Rust TSan requires a **nightly** toolchain plus `rust-src` (for `-Zbuild-std`, so
the standard library is rebuilt with instrumentation). If either is missing the
gate reports a **setup error (exit 2)** — it never silently passes: "could not run
TSan" must not be mistaken for "TSan clean".

Note on allocators: TSan intercepts the system allocator, which conflicts with a
custom `#[global_allocator]`. The `bfs` example does not install mimalloc (only
`spines`/`scc`/`columnar` do), so bfs builds under TSan without swapping it out. If
a future candidate adds a global allocator to the bfs path, set
`DD_TSAN_NO_MIMALLOC=1` in that build (the gate passes it through as a cfg).

Exit 0 = PASS (TSan reported no race on the multi-worker smoke run);
1 = a race was reported (hard fail);
2 = a build/setup error, including nightly/rust-src unavailable.

Usage (the Judge runs this alongside the equivalence + determinism gates):
  python3 accuracy_checker/sanitizer_gate.py \
      --manifest-path engine/Cargo.toml
"""

from __future__ import annotations

import argparse
import os
import subprocess

# TSan is ~10-30x slower and rebuilds std, so we exercise the parallel paths with a
# SMALL graph rather than the metric workload — the goal is to trip races on the
# multi-worker merge/exchange paths, not to be exhaustive. `-w 4` is essential:
# TSan only sees a race when >1 worker thread runs.
_SMOKE_ARGS = ["5000", "50000", "200", "5", "inspect", "-w", "4"]
_BUILD_TIMEOUT_S = 1800
_RUN_TIMEOUT_S = 900

# Substrings that mean TSan found something (stderr).
_RACE_MARKERS = ("WARNING: ThreadSanitizer", "data race", "ThreadSanitizer: ")


def _nightly_ok():
    """Return (ok, host_triple_or_error). Needs nightly cargo + rust-src."""
    v = subprocess.run(["cargo", "+nightly", "--version"], capture_output=True, text=True)
    if v.returncode != 0:
        return False, "nightly toolchain not available (`cargo +nightly` failed)"
    comp = subprocess.run(
        ["rustup", "component", "list", "--toolchain", "nightly", "--installed"],
        capture_output=True,
        text=True,
    )
    if "rust-src" not in comp.stdout:
        return False, "rust-src not installed for nightly (needed for -Zbuild-std)"
    hv = subprocess.run(["rustc", "-vV"], capture_output=True, text=True)
    host = next(
        (
            ln.split("host:", 1)[1].strip()
            for ln in hv.stdout.splitlines()
            if ln.startswith("host:")
        ),
        None,
    )
    if not host:
        return False, "could not determine host target triple"
    return True, host


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--manifest-path",
        default="engine/Cargo.toml",
        help="Cargo.toml of the candidate engine workspace to build under TSan",
    )
    args = ap.parse_args()
    manifest = args.manifest_path

    print("sanitizer gate — build bfs under ThreadSanitizer, run multi-worker, fail on any race")
    print(f"  manifest : {manifest}")
    print(f"  smoke    : bfs {' '.join(_SMOKE_ARGS)}")
    print("-" * 72)

    ok, host_or_err = _nightly_ok()
    if not ok:
        print(f"SANITIZER: SETUP-ERROR — {host_or_err}")
        print("  (this is exit 2, NOT a pass: an un-runnable sanitizer must not read as clean)")
        return 2
    host = host_or_err

    env = dict(os.environ)
    env["RUSTFLAGS"] = (env.get("RUSTFLAGS", "") + " -Zsanitizer=thread").strip()
    # Deterministic, loud TSan: report races, keep going so we see them all.
    env["TSAN_OPTIONS"] = "halt_on_error=0 exitcode=99 " + env.get("TSAN_OPTIONS", "")

    build = [
        "cargo",
        "+nightly",
        "build",
        "-Zbuild-std",
        "--release",
        "--example",
        "bfs",
        "-p",
        "differential-dataflow",
        "--target",
        host,
        "--manifest-path",
        manifest,
    ]
    print(f"  building : {' '.join(build)}")
    try:
        b = subprocess.run(build, capture_output=True, text=True, timeout=_BUILD_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        print(f"SANITIZER: SETUP-ERROR — TSan build timed out after {_BUILD_TIMEOUT_S}s")
        return 2
    if b.returncode != 0:
        print(
            f"SANITIZER: SETUP-ERROR — TSan build failed:\n{(b.stderr or b.stdout).strip()[-800:]}"
        )
        return 2

    manifest_dir = os.path.dirname(os.path.abspath(manifest))
    tsan_bin = os.path.join(manifest_dir, "target", host, "release", "examples", "bfs")
    if not os.path.exists(tsan_bin):
        print(f"SANITIZER: SETUP-ERROR — TSan build produced no binary at {tsan_bin}")
        return 2

    print(f"  running  : {tsan_bin} {' '.join(_SMOKE_ARGS)}")
    try:
        r = subprocess.run(
            [tsan_bin, *_SMOKE_ARGS],
            capture_output=True,
            text=True,
            timeout=_RUN_TIMEOUT_S,
            env=env,
        )
    except subprocess.TimeoutExpired:
        print(f"SANITIZER: SETUP-ERROR — TSan run timed out after {_RUN_TIMEOUT_S}s")
        return 2

    stderr = r.stderr or ""
    raced = any(m in stderr for m in _RACE_MARKERS)
    print("-" * 72)
    if raced:
        # Show the first race block for the Judge/agent to act on.
        idx = min((stderr.find(m) for m in _RACE_MARKERS if m in stderr), default=0)
        snippet = stderr[idx : idx + 1200]
        print("SANITIZER: FAIL — ThreadSanitizer reported a data race:\n")
        print(snippet)
        return 1
    # exitcode=99 means TSan aborted the process on a race even if markers scrolled off.
    if r.returncode == 99:
        print("SANITIZER: FAIL — process exited 99 (TSan error exitcode); a race was detected.")
        print((stderr or r.stdout or "").strip()[-800:])
        return 1
    print("SANITIZER: PASS — ThreadSanitizer reported no data race on the multi-worker smoke run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
