"""Neuron profiling toolkit — a thin, robust wrapper over AWS ``neuron-explorer``.

``neuron-explorer`` (the successor to ``neuron-profile``) captures and
analyzes NeuronCore hardware profiles. The profiling flow for a serving
workload is two steps:

1. **capture** — run the workload under ``neuron-explorer inspect``, which
   executes the given command and writes both a *system* and a *device*
   profile (NTFF + the compiled NEFF) into an output directory.
2. **analyze** — point ``neuron-explorer view`` at that directory and ask
   for a ``summary-text`` / ``summary-json`` report (engine utilization,
   top operators, DMA), or dump the raw session with ``show-session``.

This module shells out to the real tool (it does not reimplement any
analysis) and surfaces stdout/stderr verbatim so the profiler agent sees
exactly what the tool reported — including failures (e.g. no NEFF was
produced because the model never compiled onto the device).

CLI mirrors the nsys/torch harnesses so it is usable both directly
(``python neuron_profiler/analyze_neuron.py summary <dir>``) and through
the ``neuron_profiler/server.py`` MCP server.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# neuron-explorer ships under the Neuron tools dir, which isn't always on
# PATH inside a fresh container shell.
_NEURON_BIN_DIRS = ("/opt/aws/neuron/bin",)


def _explorer() -> str:
    """Resolve the ``neuron-explorer`` executable, preferring PATH."""
    found = shutil.which("neuron-explorer")
    if found:
        return found
    for d in _NEURON_BIN_DIRS:
        cand = Path(d) / "neuron-explorer"
        if cand.is_file():
            return str(cand)
    # Fall back to the bare name; the failure message will be informative.
    return "neuron-explorer"


def _run(cmd: list[str], *, timeout: int = 1800, cwd: str | None = None) -> int:
    """Run *cmd*, streaming combined output to stdout. Returns exit code."""
    print(f"$ {' '.join(cmd)}", flush=True)
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except FileNotFoundError:
        print(
            "ERROR: neuron-explorer not found. It ships with aws-neuronx-tools; "
            "ensure /opt/aws/neuron/bin is on PATH inside the container.",
        )
        return 127
    except subprocess.TimeoutExpired:
        print(f"ERROR: command timed out after {timeout}s")
        return 124
    if proc.stdout:
        print(proc.stdout)
    if proc.returncode != 0:
        print(f"(neuron-explorer exited with code {proc.returncode})")
    return proc.returncode


def _find_one(directory: Path, suffix: str) -> Path | None:
    matches = sorted(directory.rglob(f"*{suffix}"))
    return matches[0] if matches else None


def _session_dir(report: str) -> Path:
    p = Path(report)
    if p.is_dir():
        return p
    return p.parent


# ---------------------------------------------------------------------------
# Subcommands (each prints; the MCP server captures stdout)
# ---------------------------------------------------------------------------


def cmd_capture(ns) -> None:
    """Capture a system + device profile around a workload command.

    Runs ``neuron-explorer inspect -o <out_dir> <workload>``. The workload
    should *launch and exercise* a Neuron model (so a NEFF is compiled and
    executed) — e.g. a benchmark driver hitting an already-running server,
    or a script that runs a few compiled forwards.
    """
    out_dir = Path(ns.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    workload = ns.workload
    if not workload:
        print("ERROR: --workload is required (the command that drives the model).")
        return
    # inspect takes the user script as trailing args; run it via bash -lc so
    # the agent can pass a full pipeline / env-prefixed command as one string.
    cmd = [
        _explorer(), "inspect",
        "-o", str(out_dir),
        "bash", "-lc", workload,
    ]
    # Run with cwd=out_dir: neuron-explorer drops stray artifacts
    # (system_profile.json, ntrace.pb) into the *current directory* regardless
    # of -o. If that's the git-tracked /workspace, those root-owned, mode-600
    # files break the host-side per-round `git add -A`. Keeping cwd in the
    # out-dir (under /tmp) contains them.
    rc = _run(cmd, timeout=ns.timeout, cwd=str(out_dir))
    ntff = _find_one(out_dir, ".ntff")
    neff = _find_one(out_dir, ".neff")
    print("\n--- capture artifacts ---")
    print(f"output dir : {out_dir}")
    print(f"NTFF       : {ntff or '(none found — was the model executed on a NeuronCore?)'}")
    print(f"NEFF       : {neff or '(none found — did the model compile + run on device?)'}")
    if rc == 0 and ntff is None:
        print(
            "WARNING: inspect succeeded but produced no NTFF. The workload likely "
            "did not run a compiled graph on the NeuronCore (CPU fallback?)."
        )


def _view(session: Path, output_format: str, extra: list[str] | None = None) -> None:
    cmd = [_explorer(), "view", "--disable-ui", "--output-format", output_format]
    neff = _find_one(session, ".neff")
    ntff = _find_one(session, ".ntff")
    if session.is_dir():
        cmd += ["-d", str(session)]
    if neff:
        cmd += ["-n", str(neff)]
    if ntff:
        cmd += ["-s", str(ntff)]
    cmd += extra or []
    _run(cmd)


def cmd_summary(ns) -> None:
    """High-level report: engine utilization, top operators, DMA totals.

    Wraps ``neuron-explorer view --output-format summary-text`` over the
    captured session directory.
    """
    session = _session_dir(ns.report)
    if not session.exists():
        print(f"ERROR: session path does not exist: {session}")
        return
    _view(session, "summary-text")


def cmd_summary_json(ns) -> None:
    """Machine-readable summary (``view --output-format summary-json``)."""
    session = _session_dir(ns.report)
    if not session.exists():
        print(f"ERROR: session path does not exist: {session}")
        return
    _view(session, "summary-json")


def cmd_operators(ns) -> None:
    """Per-operator / per-instruction breakdown from the device profile.

    Uses ``show-session -j`` (JSON) over the captured NTFF and prints the
    most expensive entries.
    """
    session = _session_dir(ns.report)
    ntff = _find_one(session, ".ntff")
    if ntff is None:
        print(f"ERROR: no .ntff found under {session}")
        return
    cmd = [_explorer(), "show-session", "-s", str(ntff), "-j"]
    # Capture JSON to summarize the top entries rather than dumping it whole.
    print(f"$ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR running show-session: {exc}")
        return
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        return
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # Not JSON we can parse — show it raw so the agent still gets signal.
        print(proc.stdout[:20000])
        return
    print(json.dumps(data, indent=2)[:20000])


def cmd_show(ns) -> None:
    """Raw session info (``show-session``), optionally with DMA/trace."""
    session = _session_dir(ns.report)
    ntff = _find_one(session, ".ntff")
    if ntff is None:
        print(f"ERROR: no .ntff found under {session}")
        return
    cmd = [_explorer(), "show-session", "-s", str(ntff)]
    if ns.dma:
        cmd.append("--show-dma")
    if ns.trace:
        cmd.append("--show-trace")
    _run(cmd)


def cmd_view(ns) -> None:
    """Escape hatch: pass an explicit ``--output-format`` to ``view``."""
    session = _session_dir(ns.report)
    if not session.exists():
        print(f"ERROR: session path does not exist: {session}")
        return
    _view(session, ns.output_format)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="analyze_neuron",
        description="Capture and analyze NeuronCore profiles via neuron-explorer.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("capture", help="Run a workload under neuron-explorer inspect.")
    p.add_argument("--out-dir", default="/tmp/neuronprof", help="Output dir for profiles.")
    p.add_argument("--workload", required=True, help="Shell command that drives the model.")
    p.add_argument("--timeout", type=int, default=1800)
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("summary", help="summary-text report over a captured session.")
    p.add_argument("report", help="Capture out-dir (or a file inside it).")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("summary-json", help="summary-json report over a captured session.")
    p.add_argument("report")
    p.set_defaults(func=cmd_summary_json)

    p = sub.add_parser("operators", help="Top operators/instructions (show-session JSON).")
    p.add_argument("report")
    p.set_defaults(func=cmd_operators)

    p = sub.add_parser("dma", help="Raw DMA trace (show-session --show-dma).")
    p.add_argument("report")
    p.set_defaults(func=lambda ns: cmd_show(_with(ns, dma=True, trace=False)))

    p = sub.add_parser("show", help="Raw session info (show-session).")
    p.add_argument("report")
    p.add_argument("--dma", action="store_true")
    p.add_argument("--trace", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("view", help="neuron-explorer view with an explicit output format.")
    p.add_argument("report")
    p.add_argument(
        "--output-format",
        default="summary-text",
        choices=["db", "summary-text", "summary-json", "json", "perfetto", "parquet"],
    )
    p.set_defaults(func=cmd_view)

    return parser


def _with(ns, **overrides):
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def main(argv: list[str] | None = None) -> int:
    # Make sure the Neuron tools dir is reachable for child processes too.
    for d in _NEURON_BIN_DIRS:
        if Path(d).is_dir() and d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + d
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
