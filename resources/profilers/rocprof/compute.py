#!/usr/bin/env python3
r"""rocprof-compute (kernel-altitude counters) toolkit — subcommand-based.

Wraps AMD's ROCm Compute Profiler (``rocprof-compute``, formerly Omniperf): a
hardware-counter profiler that replays a profiled command to collect
per-kernel counters, then derives System Speed-of-Light / Top-Stats /
Roofline tables from them. Verified on a real MI210 (gfx90a) against
rocprof-compute **3.1.0**: despite the name, ``profile`` shells out to the
**deprecated legacy ``rocprof`` (v1/v2)** internally, not ``rocprofv3`` --
every pass prints ROCm's own "phasing out ... rocprof/rocprofv2 in favor of
rocprofiler-sdk/rocprofv3" deprecation warning. Expect this module to need an
update whenever a ROCm release removes legacy ``rocprof`` entirely.

Usage:
    python compute.py doctor
    python compute.py profile --name run --out ./rocprof_out -- python driver.py
    python compute.py profile --name run --out ./rocprof_out \\
        --kernel 'gemm.*' --dispatch 3 --timeout 900 -- python driver.py
    python compute.py analyze ./rocprof_out/workloads/run \\
        --blocks 0,2,4 --max-stat 10

``doctor`` never installs anything; it only diagnoses. ``profile`` runs the
profiled command in its own process group and kills the whole subtree on
timeout, SIGTERM, or SIGINT, so a stuck child is never orphaned, and fails
loudly (rather than handing back an empty workload) when the kernel/dispatch
filter matched nothing. ``analyze`` falls back to reading the workload's raw
CSVs directly if the ``analyze`` phase itself fails (most often the
dependency gate below), clearly labeled as unprocessed data.

Analyze blocks (rocprof-compute's own numbering, confirmed against a real
analyze run): ``0`` Top Stats (per-kernel time breakdown), ``2`` System
Speed-of-Light (per-engine % of peak), ``4`` Roofline (arithmetic intensity
vs. empirical peak). The default set covers all three; pass ``--blocks`` to
narrow it.

**The silent-empty-output pitfall.** ``-k``/``--kernel`` filters by literal
kernel-name substring. torch GEMMs dispatch through rocBLAS/hipBLASLt as
Tensile kernels named e.g. ``Cijk_Ailk_Bljk_BBS_BH_..._MT256x128x32_...`` --
they do **not** contain the substring ``"gemm"``, so a naive ``-k gemm``
filter matches zero dispatches. Every counter-collection pass then silently
comes back with "0 contexts collected" and a header-only CSV, and
rocprof-compute's own post-processing crashes later with a confusing
``pandas.errors... KeyError: 'Grid_Size'`` deep inside ``join_prof()`` --
the empty match, not a pandas-version issue, is almost always the real
cause. ``profile`` here detects this pattern (from the subprocess log and/or
an empty resulting workload) and fails with an actionable message instead of
forwarding the raw traceback.

The dependency gate: rocprof-compute's launcher runs an all-or-nothing
``verify_deps()`` preflight and refuses to run under an interpreter missing
any package in its ``requirements.txt``, or with pandas>=3 (its CSV
conversion silently breaks on pandas 3). Keeping those dependencies out of a
serving/torch interpreter is normal, so this module searches several
candidate interpreters rather than assuming its own.

Environment overrides:
    VIBESYS_ROCPROF_COMPUTE_BIN     path to the rocprof-compute launcher
                                     (skips the PATH / $ROCM_PATH search)
    VIBESYS_ROCPROF_COMPUTE_PYTHON  interpreter whose deps satisfy
                                     rocprof-compute's dependency gate
                                     (e.g. a private venv's python; see
                                     `doctor` for a recipe)
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import ctypes.util
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROCPROF_COMPUTE_BIN_ENV = "VIBESYS_ROCPROF_COMPUTE_BIN"
ROCPROF_COMPUTE_PYTHON_ENV = "VIBESYS_ROCPROF_COMPUTE_PYTHON"

# Top Stats, System Speed-of-Light, Roofline.
DEFAULT_ANALYZE_BLOCKS = ("0", "2", "4")
DEFAULT_MAX_STAT = 10
DEFAULT_PROFILE_TIMEOUT = 1800.0
DEFAULT_ANALYZE_TIMEOUT = 300.0
DEFAULT_PREFLIGHT_TIMEOUT = 60.0
DEFAULT_DEPS_CHECK_TIMEOUT = 60.0
DEFAULT_PANDAS_CHECK_TIMEOUT = 30.0
DEFAULT_ANALYZE_CLEAN_MAX_LINES = 500

# rocprof-compute `profile -b`/`--block` hardware-block choices (profile help text);
# distinct from analyze's `-b` numeric section ids in DEFAULT_ANALYZE_BLOCKS above.
_HARDWARE_BLOCKS = ("SQ", "SQC", "TA", "TD", "TCP", "TCC", "SPI", "CPC", "CPF")

_AQLPROFILE_LIB_NAMES = ("libaqlprofile64.so", "libaqlprofile.so")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_DECORATION_RE = re.compile(r"^[=\-_*]{5,}$")
# The ROCm ASCII-art banner rocprof-compute prints at the top of every
# invocation: only whitespace and shape-drawing ASCII (no alnum), which
# distinguishes it from the Unicode box-drawing table borders we want to keep.
_BANNER_ART_RE = re.compile(r"^[\s_/\\|().,'`]+$")
_CONTEXTS_COLLECTED_RE = re.compile(r"(\d+) contexts collected")
_EMPTY_JOIN_KEYERROR = "KeyError: 'Grid_Size'"
_WORKLOAD_KERNEL_CSVS = ("pmc_kernel_top.csv", "pmc_perf.csv")

# ---------------------------------------------------------------------------
# Locating the tool
# ---------------------------------------------------------------------------


def _rocm_path_roots() -> list[str]:
    roots = [os.environ.get("ROCM_PATH", "").strip(), "/opt/rocm"]
    return [root for root in roots if root]


def find_rocprof_compute_bin() -> str | None:
    """Locate the rocprof-compute launcher.

    Search order: ``VIBESYS_ROCPROF_COMPUTE_BIN`` override, PATH, then
    ``<$ROCM_PATH or /opt/rocm>/bin/rocprof-compute``.
    """
    override = os.environ.get(ROCPROF_COMPUTE_BIN_ENV, "").strip()
    if override:
        return override if os.access(override, os.X_OK) else None
    on_path = shutil.which("rocprof-compute")
    if on_path:
        return on_path
    for root in _rocm_path_roots():
        candidate = Path(root) / "bin" / "rocprof-compute"
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def find_rocprofv3_bin() -> str | None:
    """Locate ``rocprofv3``.

    Used only by this module's optional torch-import preflight, not by
    rocprof-compute itself -- see ``find_rocprof_legacy_bin``.
    """
    on_path = shutil.which("rocprofv3")
    if on_path:
        return on_path
    for root in _rocm_path_roots():
        candidate = Path(root) / "bin" / "rocprofv3"
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def find_rocprof_legacy_bin() -> str | None:
    """Locate the deprecated legacy ``rocprof`` (v1/v2) binary.

    Confirmed on a real MI210 run (rocprof-compute 3.1.0): ``profile`` shells
    out to this tool internally, not ``rocprofv3`` -- every counter-collection
    pass logs ROCm's own deprecation warning for it. It ships alongside
    ``rocprof-compute``/``rocprofv3`` in the same ROCm ``bin/`` directory.
    """
    on_path = shutil.which("rocprof")
    if on_path:
        return on_path
    for root in _rocm_path_roots():
        candidate = Path(root) / "bin" / "rocprof"
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def find_aqlprofile_lib() -> str | None:
    """Locate the aqlprofile shared library the profiler loads as its HSA tool."""
    for root in _rocm_path_roots():
        for name in _AQLPROFILE_LIB_NAMES:
            candidate = Path(root) / "lib" / name
            if candidate.is_file():
                return str(candidate)
    return ctypes.util.find_library("aqlprofile64") or ctypes.util.find_library("aqlprofile")


# ---------------------------------------------------------------------------
# The dependency gate
# ---------------------------------------------------------------------------


def _shebang_python(path: str) -> str | None:
    """The interpreter a script's ``#!`` shebang names, if any."""
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as f:
            first_line = f.readline()
    except OSError:
        return None
    if not first_line.startswith("#!"):
        return None
    return first_line[2:].strip().split(" ", 1)[0] or None


def candidate_interpreters(rocprof_bin: str) -> list[str]:
    """Interpreters to try, in preference order.

    ``VIBESYS_ROCPROF_COMPUTE_PYTHON`` override, the tool's own shebang
    interpreter (if any), the current interpreter, then ``python3`` on PATH.
    """
    seen: set[str] = set()
    ordered: list[str] = []

    def add(python: str | None) -> None:
        python = (python or "").strip()
        if python and python not in seen:
            seen.add(python)
            ordered.append(python)

    add(os.environ.get(ROCPROF_COMPUTE_PYTHON_ENV))
    add(_shebang_python(rocprof_bin))
    add(sys.executable)
    add(shutil.which("python3"))
    add("/usr/bin/python3")
    return ordered


def _python_satisfies_deps(python: str, rocprof_bin: str) -> bool:
    """True iff ``python`` can run the rocprof-compute CLI at all.

    ``--help`` is enough to trip ``verify_deps()`` without doing any real
    profiling work.
    """
    try:
        result = subprocess.run(  # noqa: S603  # LW-920129; the subprocess argv is a fixed sequence built by this code, not attacker-controlled shell input
            [python, rocprof_bin, "--help"],
            capture_output=True,
            timeout=DEFAULT_DEPS_CHECK_TIMEOUT,
            text=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def find_deps_python(rocprof_bin: str) -> str | None:
    """First candidate interpreter whose deps satisfy the dependency gate."""
    for python in candidate_interpreters(rocprof_bin):
        if _python_satisfies_deps(python, rocprof_bin):
            return python
    return None


def check_pandas_version(python: str) -> tuple[bool, str]:
    """Check pandas is importable under ``python`` and its major version < 3.

    rocprof-compute's CSV-to-report conversion silently breaks on pandas>=3.
    """
    try:
        result = subprocess.run(  # noqa: S603  # LW-920130; the subprocess argv is a fixed sequence built by this code, not attacker-controlled shell input
            [python, "-c", "import pandas; print(pandas.__version__)"],
            capture_output=True,
            text=True,
            timeout=DEFAULT_PANDAS_CHECK_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not run pandas under {python}: {exc}"
    if result.returncode != 0:
        return False, f"pandas not importable under {python}: {result.stderr.strip()[-300:]}"
    version = result.stdout.strip()
    major = version.split(".", 1)[0]
    if not major.isdigit():
        return False, f"unparsable pandas version {version!r} under {python}"
    if int(major) >= 3:  # noqa: PLR2004  # LW-920131; this is a well-known, self-explanatory constant from the file format/tool being parsed
        return False, f"pandas {version} under {python} is >=3 (rocprof-compute needs pandas<3)"
    return True, f"pandas {version} under {python}"


def _private_venv_recipe() -> str:
    return (
        "Build a private venv so the dependency gate passes without touching "
        "the torch/system interpreter: `python3 -m venv /opt/rocprof-compute-venv "
        "&& /opt/rocprof-compute-venv/bin/pip install rocprof-compute 'pandas<3'` "
        "(or point pip at rocprof-compute's own requirements.txt if it was "
        f"installed from source), then `export {ROCPROF_COMPUTE_PYTHON_ENV}="
        "/opt/rocprof-compute-venv/bin/python`."
    )


# ---------------------------------------------------------------------------
# Subcommand -- doctor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DoctorCheck:
    """One doctor check's outcome: a status line, and a fix if it failed."""

    status: str  # "OK" | "FAIL" | "SKIP"
    line: str
    fix: str | None = None


_STATUS_PREFIXES = {"OK": "[OK]  ", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}


def _check_rocprof_bin() -> tuple[_DoctorCheck, str | None]:
    rocprof_bin = find_rocprof_compute_bin()
    if rocprof_bin:
        return _DoctorCheck("OK", f"rocprof-compute binary: {rocprof_bin}"), rocprof_bin
    fix = (
        f"Set ${ROCPROF_COMPUTE_BIN_ENV} to its path, add it to PATH, or install it under "
        "$ROCM_PATH/bin (or /opt/rocm/bin)."
    )
    return _DoctorCheck("FAIL", "rocprof-compute binary: not found", fix), None


def _check_deps_python(rocprof_bin: str | None) -> tuple[_DoctorCheck, str | None]:
    if not rocprof_bin:
        return _DoctorCheck(
            "SKIP", "deps interpreter: no rocprof-compute binary to check against"
        ), None
    deps_python = find_deps_python(rocprof_bin)
    if deps_python:
        return _DoctorCheck("OK", f"deps interpreter: {deps_python}"), deps_python
    fix = "deps interpreter: no candidate interpreter satisfies the dependency gate"
    return _DoctorCheck("FAIL", fix, _private_venv_recipe()), None


def _check_pandas(deps_python: str | None) -> _DoctorCheck:
    if not deps_python:
        return _DoctorCheck("SKIP", "pandas: no deps interpreter to check under")
    pandas_ok, pandas_msg = check_pandas_version(deps_python)
    if pandas_ok:
        return _DoctorCheck("OK", f"pandas: {pandas_msg}")
    fix = f"Install pandas<3 into {deps_python}: `{deps_python} -m pip install 'pandas<3'`."
    return _DoctorCheck("FAIL", f"pandas: {pandas_msg}", fix)


def _check_rocprof_legacy() -> _DoctorCheck:
    # rocprof-compute's own internal dependency (confirmed on a real capture:
    # every pass shells to this, not rocprofv3 -- see find_rocprof_legacy_bin).
    rocprof_bin = find_rocprof_legacy_bin()
    if rocprof_bin:
        return _DoctorCheck(
            "OK", f"legacy rocprof (rocprof-compute's internal driver): {rocprof_bin}"
        )
    fix = (
        "Install the ROCm profiler package that ships the deprecated legacy `rocprof` "
        "(v1/v2), or add it to PATH -- rocprof-compute 3.1.0 shells out to it internally, "
        "not rocprofv3, so `profile` cannot run without it."
    )
    return _DoctorCheck("FAIL", "legacy rocprof: not found on PATH or under $ROCM_PATH/bin", fix)


def _check_aqlprofile() -> _DoctorCheck:
    aqlprofile_lib = find_aqlprofile_lib()
    if aqlprofile_lib:
        return _DoctorCheck("OK", f"aqlprofile library: {aqlprofile_lib}")
    fix = (
        "Ensure libaqlprofile64.so is installed under $ROCM_PATH/lib (or /opt/rocm/lib) "
        "or is otherwise on the dynamic loader's search path."
    )
    return _DoctorCheck("FAIL", "aqlprofile library: not resolvable", fix)


_VERSION_RE = re.compile(r"rocprofiler-compute version:\s*(\S+)")


def _check_version(rocprof_bin: str | None, deps_python: str | None) -> _DoctorCheck:
    """Informational: report the installed version (confirmed working: 3.1.0)."""
    if not rocprof_bin or not deps_python:
        return _DoctorCheck("SKIP", "version: no runnable rocprof-compute to check")
    try:
        result = subprocess.run(  # noqa: S603  # LW-920132; the subprocess argv is a fixed sequence built by this code, not attacker-controlled shell input
            [deps_python, rocprof_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=DEFAULT_DEPS_CHECK_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _DoctorCheck("SKIP", f"version: could not run --version ({exc})")
    match = _VERSION_RE.search(result.stdout)
    if not match:
        return _DoctorCheck("SKIP", "version: unparsable --version output")
    version = match.group(1)
    note = "" if version.startswith("3.") else " (verified against 3.1.0; other majors may differ)"
    return _DoctorCheck("OK", f"version: {version}{note}")


def cmd_doctor(ns: argparse.Namespace) -> None:
    """Diagnose the rocprof-compute install and print actionable fixes."""
    del ns
    bin_check, rocprof_bin = _check_rocprof_bin()
    deps_check, deps_python = _check_deps_python(rocprof_bin)
    checks = [
        bin_check,
        deps_check,
        _check_pandas(deps_python),
        _check_version(rocprof_bin, deps_python),
        _check_rocprof_legacy(),
        _check_aqlprofile(),
    ]

    print("\n".join(f"{_STATUS_PREFIXES[c.status]} {c.line}" for c in checks))  # noqa: T201  # LW-920133; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    fixes = [c.fix for c in checks if c.fix]
    if fixes:
        print("\nFixes:")  # noqa: T201  # LW-920134; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        for i, fix in enumerate(fixes, 1):
            print(f"  {i}. {fix}")  # noqa: T201  # LW-920135; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    else:
        print("\nAll checks passed.")  # noqa: T201  # LW-920136; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism


# ---------------------------------------------------------------------------
# Process lifecycle — own process group, kill the whole tree on timeout/signal
# ---------------------------------------------------------------------------

# The in-flight child, so an external SIGTERM/SIGINT (e.g. the agent's own
# `timeout` wrapper) reaps its whole subtree instead of orphaning it.
_ACTIVE_PROC: subprocess.Popen | None = None


def _descendant_pids(root_pid: int) -> list[int]:
    """Every ``/proc``-visible descendant of ``root_pid``, best-effort."""
    parent_of: dict[int, int] = {}
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            ppid = int(stat.rsplit(")", 1)[-1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        parent_of[pid] = ppid
    children: dict[int, list[int]] = {}
    for pid, ppid in parent_of.items():
        children.setdefault(ppid, []).append(pid)
    stack, seen, out = list(children.get(root_pid, [])), set(), []
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def kill_process_tree(pid: int) -> None:
    """SIGKILL a process, every descendant, and its process group."""
    for descendant in _descendant_pids(pid):
        with contextlib.suppress(OSError):
            os.kill(descendant, signal.SIGKILL)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def _terminate_active(signum: int, _frame: object) -> None:
    if _ACTIVE_PROC is not None:
        kill_process_tree(_ACTIVE_PROC.pid)
    os._exit(128 + signum)


def install_signal_handlers() -> None:
    """Reap the in-flight child's subtree on external SIGTERM/SIGINT."""
    signal.signal(signal.SIGTERM, _terminate_active)
    signal.signal(signal.SIGINT, _terminate_active)


def run_with_timeout(
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float,
) -> tuple[int, str]:
    """Run ``cmd`` in its own process group; kill the whole tree on timeout."""
    global _ACTIVE_PROC  # noqa: PLW0603  # LW-920137; this module holds intentional process-lifetime state (a capture slot) mutated across calls
    proc = subprocess.Popen(  # noqa: S603  # LW-920138; the subprocess argv is a fixed sequence built by this code, not attacker-controlled shell input
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    _ACTIVE_PROC = proc
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        with contextlib.suppress(Exception):
            out, _ = proc.communicate(timeout=30)
        return 124, f"TIMEOUT after {timeout}s\n{out or ''}"
    finally:
        _ACTIVE_PROC = None
    return proc.returncode, out or ""


def check_torch_import_under_rocprofv3(
    python: str, *, timeout: float = DEFAULT_PREFLIGHT_TIMEOUT
) -> tuple[bool, str]:
    """Fast preflight: does ``python -c 'import torch'`` survive under rocprofv3?

    Some images collide (e.g. an LLVM option registered twice) when
    rocprofv3 and torch share a process; this catches that before a long
    ``profile`` run burns its timeout on it. Note: rocprof-compute 3.1.0 has
    been confirmed to shell out to the deprecated *legacy* ``rocprof``
    internally, not ``rocprofv3`` (see the module docstring), so this is a
    best-effort generic canary for a torch/profiler-in-same-process collision
    -- not a literal rehearsal of what ``profile`` itself will run.
    """
    rocprofv3 = find_rocprofv3_bin()
    if not rocprofv3:
        return True, "rocprofv3 not found; skipping preflight"
    cmd = [rocprofv3, "--hip-trace", "--", python, "-c", "import torch"]
    rc, out = run_with_timeout(cmd, timeout=timeout)
    if rc == 0:
        return True, ""
    return False, out[-1000:]


# ---------------------------------------------------------------------------
# Subcommand -- profile
# ---------------------------------------------------------------------------


def _resolve_profiled_cmd(raw_cmd: list[str]) -> list[str]:
    """Strip a leading ``--`` separator; exit if nothing follows it."""
    cmd = raw_cmd[1:] if raw_cmd[:1] == ["--"] else list(raw_cmd)
    if not cmd:
        sys.exit("profile: no command given after `--`")
    return cmd


def _require_rocprof_compute() -> tuple[str, str]:
    """Resolve ``(rocprof_bin, deps_python)``, or exit with an actionable message."""
    rocprof_bin = find_rocprof_compute_bin()
    if not rocprof_bin:
        sys.exit(
            "rocprof-compute not found. Run `python compute.py doctor` for how to install or "
            f"point at it (or set ${ROCPROF_COMPUTE_BIN_ENV})."
        )
    python = find_deps_python(rocprof_bin)
    if not python:
        sys.exit(
            "rocprof-compute is installed, but no interpreter satisfies its dependency gate. "
            "Run `python compute.py doctor` for a fix."
        )
    return rocprof_bin, python


def _maybe_preflight_torch_import(ns: argparse.Namespace, cmd: list[str]) -> None:
    if not ns.check_torch_import:
        return
    ok, detail = check_torch_import_under_rocprofv3(cmd[0])
    if ok:
        return
    print(f"[FAIL] rocprofv3 cannot import torch under {cmd[0]!r}; not profiling.")  # noqa: T201  # LW-920139; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(detail)  # noqa: T201  # LW-920140; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    sys.exit(3)


def _build_profile_cmd(
    python: str,
    rocprof_bin: str,
    ns: argparse.Namespace,
    cmd: list[str],
    workload_dir: Path,
) -> list[str]:
    # `-p` is explicit and points directly at the final workload directory:
    # confirmed on a real MI210 run that rocprof-compute 3.1.0 writes CSVs
    # straight into whatever `-p` names, with no extra `<gpu>` subdirectory
    # layer -- relying on the tool's own cwd-relative default (and then
    # globbing for a results subdirectory) picks up the wrong directory (see
    # `_report_profile_result`'s history for why that glob was removed).
    profile_cmd = [python, rocprof_bin, "profile", "-n", ns.name, "-p", str(workload_dir)]
    if ns.kernel:
        profile_cmd += ["-k", ns.kernel]
    if ns.dispatch is not None:
        profile_cmd += ["--dispatch", str(ns.dispatch)]
    if ns.block:
        profile_cmd += ["-b", *ns.block]
    return [*profile_cmd, "--", *cmd]


def _log_shows_zero_contexts(log: str) -> bool:
    """True iff every counter-collection pass in the log collected zero contexts.

    That's the silent-empty-match signature: seen on a real run right before
    rocprof-compute's own post-processing crashed with
    ``KeyError: 'Grid_Size'``.
    """
    counts = [int(n) for n in _CONTEXTS_COLLECTED_RE.findall(log)]
    return bool(counts) and all(count == 0 for count in counts)


def _workload_has_kernel_data(workload_dir: Path) -> bool | None:
    """Whether the workload has at least one profiled kernel dispatch.

    Checks the first of ``_WORKLOAD_KERNEL_CSVS`` that exists (whichever
    rocprof-compute wrote) and treats "header row only" as no data, regardless
    of column order or count. Returns ``None`` -- a different failure than an
    empty match -- when none of those files exist at all.
    """
    for name in _WORKLOAD_KERNEL_CSVS:
        path = workload_dir / name
        if not path.is_file():
            continue
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        return len(rows) > 1
    return None


def _empty_match_message(kernel: str, dispatch: int | None) -> str:
    filters = []
    if kernel:
        filters.append(f"--kernel {kernel!r}")
    if dispatch is not None:
        filters.append(f"--dispatch {dispatch}")
    filter_note = f" Current filter: {', '.join(filters)}." if filters else ""
    return (
        "kernel filter matched no dispatches; list kernel names with "
        "`rocprofv3 --kernel-trace --stats` first. torch GEMMs dispatch through "
        "rocBLAS/hipBLASLt as Tensile kernels named like "
        "'Cijk_Ailk_Bljk_BBS_BH_..._MT256x128x32_...', not anything containing "
        f"the literal substring 'gemm'.{filter_note}"
    )


def _report_profile_result(
    rc: int,
    log: str,
    workload_dir: Path,
    *,
    kernel: str = "",
    dispatch: int | None = None,
) -> None:
    empty_match = _log_shows_zero_contexts(log) or _EMPTY_JOIN_KEYERROR in log

    if rc != 0:
        print("PROFILE FAILED.")  # noqa: T201  # LW-920141; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        if empty_match:
            print(_empty_match_message(kernel, dispatch))  # noqa: T201  # LW-920142; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        print(log[-2000:])  # noqa: T201  # LW-920143; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        sys.exit(1)

    if not workload_dir.is_dir():
        print("PROFILE completed but produced no workload directory.")  # noqa: T201  # LW-920144; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        print(log[-1000:])  # noqa: T201  # LW-920145; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        sys.exit(1)

    if empty_match or _workload_has_kernel_data(workload_dir) is False:
        print("PROFILE completed but matched no kernel dispatches.")  # noqa: T201  # LW-920146; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        print(_empty_match_message(kernel, dispatch))  # noqa: T201  # LW-920147; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        sys.exit(1)

    print(f"Workload written to: {workload_dir}")  # noqa: T201  # LW-920148; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(  # noqa: T201  # LW-920149; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        "Raw counters (pmc_perf.csv etc.) live under that directory. Analyze with:\n"
        f"  python compute.py analyze {workload_dir}"
    )


def cmd_profile(ns: argparse.Namespace) -> None:
    """Run ``rocprof-compute profile`` with a hard timeout, own process group."""
    cmd = _resolve_profiled_cmd(ns.cmd)
    rocprof_bin, python = _require_rocprof_compute()
    _maybe_preflight_torch_import(ns, cmd)

    out_dir = Path(ns.out)
    workload_dir = out_dir / "workloads" / ns.name
    out_dir.mkdir(parents=True, exist_ok=True)
    install_signal_handlers()

    profile_cmd = _build_profile_cmd(python, rocprof_bin, ns, cmd, workload_dir)
    rc, log = run_with_timeout(profile_cmd, timeout=ns.timeout)
    _report_profile_result(rc, log, workload_dir, kernel=ns.kernel, dispatch=ns.dispatch)


# ---------------------------------------------------------------------------
# Subcommand -- analyze
# ---------------------------------------------------------------------------


def _clean_analyze_output(text: str, *, max_lines: int = DEFAULT_ANALYZE_CLEAN_MAX_LINES) -> str:
    """Strip ANSI escapes, the ROCm ASCII-art banner, and separator decoration.

    Also caps the total line count so an unfiltered (all-18-section) analyze
    dump still stays prompt-sized. Unicode box-drawing table borders
    (``╒═╤``, ...) are untouched -- only the ASCII-art banner and
    ``----``/``====`` rules are decoration here.
    """
    cleaned_lines = []
    for raw_line in _ANSI_RE.sub("", text).splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if _DECORATION_RE.match(stripped) or (stripped and _BANNER_ART_RE.match(stripped)):
            continue
        cleaned_lines.append(line)
    while cleaned_lines and not cleaned_lines[0].strip():
        cleaned_lines.pop(0)
    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()
    if len(cleaned_lines) > max_lines:
        omitted = len(cleaned_lines) - max_lines
        cleaned_lines = [*cleaned_lines[:max_lines], f"... ({omitted} more lines truncated) ..."]
    return "\n".join(cleaned_lines)


def _find_workload_csvs(workload: str) -> list[Path]:
    return sorted(Path(workload).rglob("*.csv"))


def _shorten_kernel_name(name: str, *, max_len: int = 80) -> str:
    return name if len(name) <= max_len else f"{name[: max_len - 3]}..."


def _format_percent(raw: str) -> str:
    """Round a percentage string to 1 decimal place; pass through unparsable input."""
    try:
        return f"{float(raw):.1f}"
    except ValueError:
        return raw


def _print_top_kernels_from_csv(path: Path, *, max_rows: int) -> bool:
    """Print rocprof-compute's own precomputed per-kernel time breakdown.

    ``pmc_kernel_top.csv`` (Kernel_Name, Count, Sum(ns), Mean(ns), Median(ns),
    Pct) is written during ``profile`` itself, already sorted by time -- no
    aggregation needed here, just formatting. Column access is by name (not
    position), so column order/additions in a future rocprof-compute version
    don't break this. Returns whether anything was printed.
    """
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return False
    print(f"\n--- top kernels by time ({path.name}) ---")  # noqa: T201  # LW-920150; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    for row in rows[:max_rows]:
        name = _shorten_kernel_name(row.get("Kernel_Name", "?"))
        pct = _format_percent(row.get("Pct", "?"))
        count = row.get("Count", "?")
        total_ns = row.get("Sum(ns)", "?")
        print(f"  {pct:>6}%  {name}  (count={count}, sum={total_ns}ns)")  # noqa: T201  # LW-920151; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    if len(rows) > max_rows:
        print(f"  ... ({len(rows) - max_rows} more kernels omitted)")  # noqa: T201  # LW-920152; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    return True


def _sum_column(rows: list[dict[str, str]], column: str) -> float | None:
    """Sum a named column across rows.

    Tolerates missing/blank/non-numeric values (e.g. a counter set that
    didn't collect that column). Returns ``None`` if the column contributed
    no numeric value at all.
    """
    total = 0.0
    seen = False
    for row in rows:
        raw = row.get(column)
        if not raw:
            continue
        try:
            total += float(raw)
        except ValueError:
            continue
        seen = True
    return total if seen else None


def _print_pmc_perf_ratios(path: Path) -> bool:
    """Print a couple of SoL-style ratios from raw ``pmc_perf.csv`` counters.

    Computed by summing each counter across every dispatch. These are rough
    and unweighted (not rocprof-compute's own peak-normalized Speed-of-Light
    numbers, which ``analyze`` derives) -- useful as a cheap signal when
    ``analyze`` itself isn't available. Returns whether anything was printed.
    """
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return False
    hits = _sum_column(rows, "TCC_HIT_sum")
    misses = _sum_column(rows, "TCC_MISS_sum")
    valu = _sum_column(rows, "SQ_INSTS_VALU")
    mfma = _sum_column(rows, "SQ_INSTS_MFMA")

    lines = []
    if hits is not None and misses is not None and (hits + misses) > 0:
        lines.append(
            f"  L2 cache hit rate (TCC_HIT/(HIT+MISS)): {100 * hits / (hits + misses):.1f}%"
        )
    if valu is not None and mfma is not None and (valu + mfma) > 0:
        lines.append(f"  MFMA share of VALU+MFMA issue slots: {100 * mfma / (valu + mfma):.1f}%")
    if not lines:
        return False
    print(f"\n--- rough counter ratios from {path.name} (not peak-normalized) ---")  # noqa: T201  # LW-920153; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    for line in lines:
        print(line)  # noqa: T201  # LW-920154; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    return True


def _print_csv_fallback(workload: str, *, max_rows: int) -> None:
    workload_path = Path(workload)
    csvs = _find_workload_csvs(workload)
    if not csvs:
        print(f"No CSVs found under {workload} either.")  # noqa: T201  # LW-920155; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        return

    printed = False
    top_kernels = workload_path / "pmc_kernel_top.csv"
    if top_kernels.is_file() and _print_top_kernels_from_csv(top_kernels, max_rows=max_rows):
        printed = True

    pmc_perf = workload_path / "pmc_perf.csv"
    if pmc_perf.is_file() and _print_pmc_perf_ratios(pmc_perf):
        printed = True

    roofline = workload_path / "roofline.csv"
    if roofline.is_file():
        with roofline.open(newline="", encoding="utf-8") as f:
            roofline_rows = list(csv.reader(f))
        if len(roofline_rows) > 1:
            print(  # noqa: T201  # LW-920156; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
                f"\n--- {roofline.name} also present: {len(roofline_rows[0])} columns, "
                f"{len(roofline_rows) - 1} device row(s) (device peak/achieved FLOPs and "
                "bandwidth; not expanded here)"
            )
            printed = True

    if printed:
        return

    # Nothing we know how to specifically summarize -- dump the most
    # promising raw CSV, same as before, clearly marked as unprocessed.
    preferred = [c for c in csvs if c.name == "pmc_perf.csv"] or csvs[:1]
    for path in preferred:
        print(f"\n--- {path} ---")  # noqa: T201  # LW-920157; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        if not rows:
            print("(empty)")  # noqa: T201  # LW-920158; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
            continue
        header, body = rows[0], rows[1:]
        print(f"columns ({len(header)}): {', '.join(header)}")  # noqa: T201  # LW-920159; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        print(f"{len(body)} data rows; showing first {min(max_rows, len(body))}:")  # noqa: T201  # LW-920160; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        for row in body[:max_rows]:
            print("  " + ", ".join(row))  # noqa: T201  # LW-920161; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism


def cmd_analyze(ns: argparse.Namespace) -> None:
    """Run ``rocprof-compute analyze``; fall back to raw CSVs if it fails."""
    workload = ns.workload_dir
    if not Path(workload).is_dir():
        sys.exit(f"analyze: not a directory: {workload}")

    rocprof_bin = find_rocprof_compute_bin()
    python = find_deps_python(rocprof_bin) if rocprof_bin else None
    blocks = [b.strip() for b in ns.blocks.split(",") if b.strip()]

    reason = ""
    tail = ""
    if rocprof_bin and python:
        cmd = [
            python,
            rocprof_bin,
            "analyze",
            "-p",
            workload,
            "-b",
            *blocks,
            "--max-stat-num",
            str(ns.max_stat),
        ]
        if ns.kernel:
            cmd += ["-k", ns.kernel]
        rc, output = run_with_timeout(cmd, timeout=ns.timeout)
        if rc == 0:
            print(_clean_analyze_output(output))  # noqa: T201  # LW-920162; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
            return
        reason = f"`rocprof-compute analyze` exited {rc}"
        tail = output[-500:]
    else:
        reason = "rocprof-compute binary or a deps-satisfying interpreter is not available"

    print(  # noqa: T201  # LW-920163; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        f"NOTE: analyze failed ({reason}); falling back to raw CSVs under {workload} "
        "(uninterpreted -- no rocprof-compute derived metrics)."
    )
    if tail:
        print(f"analyze output tail:\n{tail}")  # noqa: T201  # LW-920164; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    _print_csv_fallback(workload, max_rows=ns.max_stat)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """Entry point: parse subcommand args and dispatch."""
    parser = argparse.ArgumentParser(
        prog="compute.py",
        description="rocprof-compute (kernel-altitude counters) toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="diagnose the rocprof-compute install")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("profile", help="run `rocprof-compute profile` with a hard timeout")
    p.add_argument("--name", required=True, help="workload name (rocprof-compute -n)")
    p.add_argument("--out", required=True, help="directory to hold workloads/<name>/...")
    p.add_argument(
        "--kernel", default="", help="substring/regex filtering kernels (rocprof-compute -k)"
    )
    p.add_argument("--dispatch", type=int, default=None, help="filter by dispatch id")
    p.add_argument(
        "--block",
        nargs="+",
        choices=_HARDWARE_BLOCKS,
        default=None,
        help="narrow capture to these hardware blocks (rocprof-compute -b), fewer passes/less overhead",
    )
    p.add_argument("--timeout", type=float, default=DEFAULT_PROFILE_TIMEOUT)
    p.add_argument(
        "--check-torch-import",
        action="store_true",
        help="preflight: does rocprofv3 let this command's interpreter import torch, before the (long) profile run",
    )
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="command to profile, preceded by --")
    p.set_defaults(fn=cmd_profile)

    p = sub.add_parser("analyze", help="run `rocprof-compute analyze`, with a raw-CSV fallback")
    p.add_argument("workload_dir")
    p.add_argument("--blocks", default=",".join(DEFAULT_ANALYZE_BLOCKS))
    p.add_argument("--max-stat", type=int, default=DEFAULT_MAX_STAT)
    p.add_argument("--kernel", default="")
    p.add_argument("--timeout", type=float, default=DEFAULT_ANALYZE_TIMEOUT)
    p.set_defaults(fn=cmd_analyze)

    ns = parser.parse_args(argv)
    ns.fn(ns)


if __name__ == "__main__":
    main()
