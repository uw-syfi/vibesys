#!/usr/bin/env python3
"""Generic, engine-agnostic op-level ``torch.profiler`` capture.

``profile_ops`` runs *any* command (a candidate torch program: an offline
script, or a server driven by a load generator) through the shared
``capture_runtime`` lifecycle, with ``inject/sitecustomize.py`` prepended to
its ``PYTHONPATH``. That module arms itself the moment the target process
imports torch and shows a visible GPU, and writes a raw Kineto/Chrome trace
per process (see its docstring for the full mechanism). This file owns
nothing engine-specific: ``command``/``env``/``ready_command``/
``load_command`` are opaque, agent-supplied shell strings, exactly like every
other ``profile_*`` tool built on ``capture_runtime``.

After the capture, this module discovers every trace the run produced
(one per process that armed), picks the *primary* one (the trace with the
most GPU kernel events — the process that actually did GPU work, in a
multi-process program), runs the torch analyzer's ``certify`` plus a compact
summary against it, and records the choice in the capture's manifest.

Usage as a CLI (mirrors ``analyze_torch_profile.py``'s subcommand style, for
tests and ad hoc use without the MCP server)::

    python capture_ops.py --command "python3 workload.py"
        [--cwd DIR] [--env KEY=VALUE ...]
        [--ready-command CMD] [--ready-timeout-s N]
        [--load-command CMD] [--stop-signal SIGINT]
        [--grace-s N] [--timeout-s N]
        [--delay-s N] [--duration-s N] [--no-record-shapes]
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# capture_runtime import shim (see its own docstring): a checkout stages it
# as a sibling ``_common/``, a materialized agent workspace as a sibling
# ``profilers_common/``.
for _name in ("_common", "profilers_common"):
    _candidate = _HERE.parent / _name
    if (_candidate / "capture_runtime.py").is_file():
        sys.path.insert(0, str(_candidate))
        break
import capture_runtime  # noqa: E402

sys.path.insert(0, str(_HERE))
import analyze_torch_profile  # noqa: E402

_INJECT_DIR = _HERE / "inject"
_TRACE_GLOB = "*.pt.trace.json.gz"
_SUMMARY_TOP = 5

__all__ = ["profile_ops"]


# ---------------------------------------------------------------------------
# Env wiring for the injected sitecustomize
# ---------------------------------------------------------------------------


def _build_capture_env(
    *,
    user_env: dict[str, str] | None,
    out_dir: Path,
    delay_s: float,
    duration_s: float | None,
    record_shapes: bool,
) -> dict[str, str]:
    """Layer the injection's env controls under any caller-supplied ``env``.

    ``PYTHONPATH`` is prepended (not replaced) with the inject directory so
    the target's own module search path still works; every other key the
    caller passes wins over our defaults on conflict.
    """
    merged: dict[str, str] = {
        "VIBESYS_TORCH_PROFILE": "1",
        "VIBESYS_TORCH_PROFILE_OUT_DIR": str(out_dir),
        "VIBESYS_TORCH_PROFILE_DELAY_S": str(delay_s),
        "VIBESYS_TORCH_PROFILE_RECORD_SHAPES": "1" if record_shapes else "0",
    }
    if duration_s is not None:
        merged["VIBESYS_TORCH_PROFILE_DURATION_S"] = str(duration_s)
    merged.update(user_env or {})

    existing_pythonpath = merged.get("PYTHONPATH") or os.environ.get("PYTHONPATH", "")
    parts = [str(_INJECT_DIR)]
    if existing_pythonpath:
        parts.append(existing_pythonpath)
    merged["PYTHONPATH"] = os.pathsep.join(parts)
    return merged


# ---------------------------------------------------------------------------
# Trace discovery + primary selection
# ---------------------------------------------------------------------------


def discover_traces(out_dir: Path) -> list[Path]:
    """Every ``<pid>.pt.trace.json.gz`` the capture's processes wrote, sorted."""
    return sorted(out_dir.rglob(_TRACE_GLOB))


def _kernel_count(path: Path) -> int | None:
    """Number of GPU kernel events in *path*, or ``None`` if unreadable/not a trace.

    Reuses the torch analyzer's own trace-indexing helpers rather than
    re-implementing Chrome-trace parsing here: this module and
    ``analyze_torch_profile.py`` are one cohesive plugin (co-staged, same
    directory), not separate architectural layers.
    """
    try:
        raw = analyze_torch_profile._read_json_maybe_gz(str(path))  # noqa: SLF001
    except (OSError, ValueError) as exc:
        print(f"[profile_ops] could not read {path}: {exc!r}", file=sys.stderr)  # noqa: T201  # tracked: #288
        return None
    if not analyze_torch_profile._is_chrome_trace(raw):  # noqa: SLF001
        return None
    index = analyze_torch_profile._index_trace(raw)  # noqa: SLF001
    return len(index.kernels)


def pick_primary_trace(trace_paths: list[Path]) -> Path | None:
    """The trace with the most GPU kernel events — the process that did GPU work.

    Ties broken by path for determinism (stable across otherwise-identical
    reruns). A trace that cannot be read/parsed loses to any readable one.
    """
    best: Path | None = None
    best_count = -1
    for path in trace_paths:
        count = _kernel_count(path)
        if count is None:
            continue
        if count > best_count or (count == best_count and best is not None and path < best):
            best_count = count
            best = path
    return best


def _record_traces_in_manifest(out_dir: Path, *, primary: Path | None, traces: list[Path]) -> None:
    manifest: dict = {}
    with contextlib.suppress(FileNotFoundError, ValueError):
        manifest = capture_runtime.load_manifest(out_dir)
    manifest["primary_trace"] = str(primary.relative_to(out_dir)) if primary else None
    manifest["trace_files"] = [str(p.relative_to(out_dir)) for p in traces]
    capture_runtime.write_manifest(out_dir, manifest)


# ---------------------------------------------------------------------------
# Compact post-capture analysis
# ---------------------------------------------------------------------------


def _run_cmd(fn, **kwargs) -> str:  # noqa: ANN001, ANN003  # tracked: #288
    """Run an ``analyze_torch_profile.cmd_*`` and capture its stdout.

    Mirrors ``server.py``'s ``_capture`` helper: several ``cmd_*`` functions
    reject bad input via ``sys.exit(message)`` rather than raising, so a
    ``SystemExit`` becomes an ``error: ...`` line instead of aborting this
    module.
    """
    ns = types.SimpleNamespace(**kwargs)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn(ns)
    except SystemExit as exc:
        return f"error: {exc}"
    return buf.getvalue() or "(no output)"


def _analyze_primary(trace_path: Path) -> str:
    sections = [
        "\n--- certify ---",
        _run_cmd(analyze_torch_profile.cmd_certify, trace=str(trace_path)),
        "\n--- top ops ---",
        _run_cmd(analyze_torch_profile.cmd_operators, report=str(trace_path), top=_SUMMARY_TOP),
        "\n--- top kernels ---",
        _run_cmd(analyze_torch_profile.cmd_kernels, report=str(trace_path), top=_SUMMARY_TOP),
        "\n--- gemm shapes ---",
        _run_cmd(
            analyze_torch_profile.cmd_gemm_shapes, trace=str(trace_path), top=_SUMMARY_TOP, out=None
        ),
    ]
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def profile_ops(  # noqa: PLR0913  # tracked: #288
    command: str,
    cwd: str | None = None,
    env: dict | None = None,
    ready_command: str | None = None,
    ready_timeout_s: float = 600.0,
    load_command: str | None = None,
    stop_signal: str = "SIGINT",
    grace_s: float = 120.0,
    timeout_s: float = 1800.0,
    delay_s: float = 0.0,
    duration_s: float | None = None,
    record_shapes: bool = True,  # noqa: FBT001, FBT002  # tracked: #288
) -> str:
    """Run *command* under the in-process torch.profiler injection.

    Wraps ``capture_runtime.run_capture`` (kind ``"ops"``) with
    ``inject/sitecustomize.py`` armed via ``PYTHONPATH`` + env, then picks
    the primary trace (most GPU kernel events) across every process the
    capture produced and runs ``certify`` plus a compact summary against it.

    ``load_command``/``ready_command``/``stop_signal``/``grace_s``/
    ``timeout_s`` have the same meaning as every other ``capture_runtime``
    lifecycle: omit ``load_command`` for a bounded script that exits on its
    own; set it (with ``ready_command``) to drive a server under load, then
    stop it with ``stop_signal`` (default ``SIGINT``) once the load finishes.

    Returns prompt-sized text: the capture id, lifecycle status, the primary
    trace's certify verdict, and its top ops/kernels/GEMM shapes. The full
    manifest (every trace path, which one was primary) is written to
    ``manifest.json`` in the capture directory.
    """
    _capture_id, out_dir = capture_runtime.new_capture("ops")
    lifecycle = capture_runtime.Lifecycle(
        command=command,
        cwd=cwd,
        env=_build_capture_env(
            user_env=env,
            out_dir=out_dir,
            delay_s=delay_s,
            duration_s=duration_s,
            record_shapes=record_shapes,
        ),
        ready_command=ready_command,
        ready_timeout_s=ready_timeout_s,
        load_command=load_command,
        stop_signal=stop_signal,
        grace_s=grace_s,
        timeout_s=timeout_s,
    )
    result = capture_runtime.run_capture(
        [],
        lifecycle,
        kind="ops",
        out_dir=out_dir,
        meta={"delay_s": delay_s, "duration_s": duration_s, "record_shapes": record_shapes},
    )

    lines = [capture_runtime.format_result(result)]

    traces = discover_traces(out_dir)
    if not traces:
        _record_traces_in_manifest(out_dir, primary=None, traces=[])
        lines.append(
            f"\nno {_TRACE_GLOB} trace files were produced; see the target log tail above "
            "(common cause: the process never imported torch, or torch.cuda.is_available() "
            "was false in it)"
        )
        return "\n".join(lines)

    primary = pick_primary_trace(traces)
    _record_traces_in_manifest(out_dir, primary=primary, traces=traces)
    if primary is None:
        lines.append(
            f"\n{len(traces)} trace file(s) found but none were readable Kineto/Chrome traces"
        )
        return "\n".join(lines)

    lines.append(f"\nprimary trace: {primary.relative_to(out_dir)} ({len(traces)} trace(s) total)")
    lines.append(_analyze_primary(primary))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_env_args(pairs: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--env expects KEY=VALUE, got {pair!r}")  # noqa: TRY003  # tracked: #288
        key, _, value = pair.partition("=")
        env[key] = value
    return env


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generic in-process torch.profiler capture (see profile_ops docstring)."
    )
    parser.add_argument("--command", required=True, help="Target command, run via bash -lc")
    parser.add_argument("--cwd", default=None)
    parser.add_argument(
        "--env", action="append", default=[], metavar="KEY=VALUE", help="May be repeated"
    )
    parser.add_argument("--ready-command", default=None)
    parser.add_argument("--ready-timeout-s", type=float, default=600.0)
    parser.add_argument("--load-command", default=None)
    parser.add_argument("--stop-signal", default="SIGINT")
    parser.add_argument("--grace-s", type=float, default=120.0)
    parser.add_argument("--timeout-s", type=float, default=1800.0)
    parser.add_argument("--delay-s", type=float, default=0.0)
    parser.add_argument("--duration-s", type=float, default=None)
    parser.add_argument(
        "--no-record-shapes", dest="record_shapes", action="store_false", default=True
    )
    args = parser.parse_args(argv)

    print(  # noqa: T201  # tracked: #288
        profile_ops(
            command=args.command,
            cwd=args.cwd,
            env=_parse_env_args(args.env) or None,
            ready_command=args.ready_command,
            ready_timeout_s=args.ready_timeout_s,
            load_command=args.load_command,
            stop_signal=args.stop_signal,
            grace_s=args.grace_s,
            timeout_s=args.timeout_s,
            delay_s=args.delay_s,
            duration_s=args.duration_s,
            record_shapes=args.record_shapes,
        )
    )


if __name__ == "__main__":
    main()
