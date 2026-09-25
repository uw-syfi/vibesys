#!/usr/bin/env python3
"""Agent-facing capture tools for the rocprof profiler plugin.

An agent with only MCP access needs to be able to profile anything of
interest (a server under its own load, an offline script, a microbenchmark)
on an AMD GPU. Each ``profile_*`` function here answers one question (whole-
run timeline, PMC counters, kernel-internal Speed-of-Light, instruction-level
stalls, a torch.profiler trace) and shares one generic capture lifecycle,
implemented once in ``capture_runtime`` (start, optionally wait-ready + load,
stop, escalate if needed). Nothing here knows about any specific serving
engine: what goes into ``lifecycle.command``/``env``/``ready_command``/
``load_command`` is chosen by the calling agent, guided by
``serving-systems/references/tooling/profiling-serving-engines.md``, not by
this module.

Every ``profile_*`` function takes a ``capture_runtime.Lifecycle`` (the
common start/ready/load/stop args) plus a few scoped args, and returns a
prompt-sized text: capture id, status, timings, validity checks, and a short
auto-summary, plus hints of the next drill-down tools. ``server.py`` is the
thin MCP boundary: it flattens ``Lifecycle`` into individual tool arguments
(required for FastMCP's per-argument JSON schema, which is what makes each
tool's args discoverable to an agent) and calls straight through to the
functions here, which stay independently unit-testable without FastMCP.

``summary``/``compare`` dispatch on a capture's ``manifest.json`` ``kind``
field (``timeline``, ``counters``, ``kernel_deep``, ``instructions``,
``ops``), written by whichever ``profile_*`` function produced it.

Standalone module, stdlib only (plus the sibling analyzer CLIs in this same
directory): imports ``capture_runtime`` via the same path shim documented in
its own docstring, since this module is staged as a sibling of
``resources/profilers/<kind>/`` too.
"""

from __future__ import annotations

import contextlib
import csv
import dataclasses
import importlib
import io
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import types
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Iterable

_HERE = Path(__file__).resolve().parent

# capture_runtime is a sibling of every profiler plugin, not just rocprof's
# own directory -- see resources/profilers/_common/capture_runtime.py's
# docstring for why this checks two names.
for _common_name in ("_common", "profilers_common"):
    _common_candidate = _HERE.parent / _common_name
    if (_common_candidate / "capture_runtime.py").is_file():
        if str(_common_candidate) not in sys.path:
            sys.path.insert(0, str(_common_candidate))
        break
import capture_runtime  # noqa: E402  # LW-920124; the sys.path setup directly above must run before this import, so it cannot sort to the top of the file

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import analyze_rocprof  # noqa: E402  # LW-920125; the sys.path setup directly above must run before this import, so it cannot sort to the top of the file
import att  # noqa: E402  # LW-920126; the sys.path setup directly above must run before this import, so it cannot sort to the top of the file
import compute  # noqa: E402  # LW-920127; the sys.path setup directly above must run before this import, so it cannot sort to the top of the file
import counters  # noqa: E402  # LW-920128; the sys.path setup directly above must run before this import, so it cannot sort to the top of the file

_ATT_MIN_ROCPROFV3_VERSION = (7, 1, 0)
_ATT_DECODER_LIB_NAME = "librocprof-trace-decoder.so"
_ATT_LIBRARY_PATH_ENV_VARS = ("VIBESYS_ROCPROF_ATT_LIBRARY_PATH", "ROCPROF_ATT_LIBRARY_PATH")
_WORKLOAD_KERNEL_CSV_NAMES = ("pmc_kernel_top.csv", "pmc_perf.csv")
_TOP_DELTA_ROWS = 15
_VERSION_TUPLE_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_ROCM_VERSION_FIELD_RE = re.compile(r"rocm_version:\s*(\d+)\.(\d+)\.(\d+)")
_COUNTER_METRIC_FIELDS = (
    "l2_hit_rate_pct",
    "achieved_bw_gb_s",
    "gpu_busy_pct",
    "mfma_issue_rate",
    "mfma_busy_fraction",
    "lds_bank_conflict_rate_pct",
)
_STATUS_SEVERITY: dict[capture_runtime.CaptureStatus, int] = {
    capture_runtime.CaptureStatus.OK: 0,
    capture_runtime.CaptureStatus.NOT_READY: 1,
    capture_runtime.CaptureStatus.LOAD_FAILED: 2,
    capture_runtime.CaptureStatus.TARGET_FAILED: 3,
    capture_runtime.CaptureStatus.TIMED_OUT: 4,
    capture_runtime.CaptureStatus.KILLED_AFTER_GRACE: 5,
    capture_runtime.CaptureStatus.CANCELLED: 6,
}

# rocprofv3 injects itself (LD_PRELOAD) into every process in the launched
# tree, not only the top-level target: a real capture observed this corrupt
# a value a `command`/`load_command` picked up via `$(...)` command
# substitution (e.g. a port number), because the small helper subprocess
# spawned just to compute that value inherited rocprofv3's tool library too,
# and the library printed a one-time diagnostic line ("Streaming Performance
# Monitor (SPM) is not supported on gfx90a devices") to that subprocess's
# stdout the moment it loaded -- landing inside the command-substitution
# result together with the real value.
#
# No profiler-side env var or flag suppresses this: confirmed live on real
# MI210 hardware (rocprofiler-sdk 1.3.2) against 7 candidate env-var combos
# plus `rocprofv3 --log-level fatal`, every one producing the identical
# banner and identical corruption. The only real fix is to
# never let a profiled `command`/`load_command` compute a value via a
# forked child in the first place: pick the value (e.g. a free port) in
# `setup_command` -- which every `profile_*` tool here runs to completion
# *before* the profiler ever starts, so nothing it spawns is ever injected
# into -- write it to a file there, then have `command` read that file back
# with the `read` builtin (`read -r VAR < file`, never `$(...)`, including
# `$(cat file)`, which just spawns another profiled child that gets injected
# into the same way). The `profiling-serving-engines.md` skill doc has the
# full pattern.


def run_cli(fn: Callable[[types.SimpleNamespace], None], **kwargs: object) -> str:
    """Run a ``cmd_*`` with an argparse-like namespace and capture its stdout.

    Several ``cmd_*`` functions reject bad input via ``sys.exit(message)``
    rather than raising; that becomes an ``error: ...`` string here instead
    of killing the process.
    """
    ns = types.SimpleNamespace(**kwargs)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn(ns)
    except SystemExit as exc:
        return f"error: {exc}"
    out = buf.getvalue()
    return out or "(no output)"


# ---------------------------------------------------------------------------
# Host capability detection
# ---------------------------------------------------------------------------


def _run(argv: list[str], *, timeout: float = 10.0) -> tuple[int | None, str]:
    """Run a short-lived command; ``rc`` is ``None`` if it could not be launched at all."""
    try:
        result = subprocess.run(  # noqa: S603  # LW-910048; the subprocess argv is a fixed sequence built by this code, not attacker-controlled shell input
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return None, "executable not found"
    except OSError as exc:
        return None, str(exc)
    except subprocess.TimeoutExpired:
        return None, "timed out"
    return result.returncode, result.stdout + result.stderr


def _parse_version(text: str) -> tuple[int, int, int] | None:
    """Extract the first ``X.Y.Z`` version triplet from free-form tool output."""
    match = _VERSION_TUPLE_RE.search(text)
    if not match:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch)


def _parse_rocm_version(text: str) -> tuple[int, int, int] | None:
    """Extract the ``rocm_version:`` field from real ``rocprofv3 --version`` output.

    Real output (ROCm 7.x) is multi-line and reports two independent
    versions: the rocprofiler-sdk-tool's own semantic version first (e.g.
    ``version: 1.3.2``), then the ROCm release it was built against on a
    separate ``rocm_version: 7.2.3`` line. Gating ``--att`` availability (a
    ROCm-release feature, not a tool-release feature) needs the latter; a
    plain first-X.Y.Z-in-the-text match picks up the former instead, which
    can read as an old version (e.g. ``1.3.2``) even when the underlying
    ROCm release supports ``--att``. Falls back to ``None`` (letting the
    caller use the generic first-match parse) when no such field is present,
    e.g. simplified/synthetic ``--version`` output with a single number.
    """
    match = _ROCM_VERSION_FIELD_RE.search(text)
    if not match:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch)


def _rocprofv3_version(rocprofv3_bin: str) -> tuple[int, int, int] | None:
    rc, output = _run([rocprofv3_bin, "--version"])
    if rc != 0:
        return None
    return _parse_rocm_version(output) or _parse_version(output)


def _torch_available() -> tuple[bool, str]:
    rc, output = _run(
        [sys.executable, "-c", "import torch; print(torch.__version__)"], timeout=20.0
    )
    if rc == 0:
        return True, output.strip()
    lines = output.strip().splitlines()
    return False, (lines[-1] if lines else "import failed")


def _parse_rocminfo_agents(text: str) -> list[dict[str, str | None]]:
    """Best-effort parse of ``rocminfo`` output into GPU agent records.

    Only ``rocminfo``'s flat ``Name:``/``Marketing Name:``/``Compute Unit:``
    fields are used; a GPU agent is any block whose ``Name:`` value starts
    with ``gfx`` (CPU agents' ``Name:`` is the CPU model string).
    """
    agents: list[dict[str, str | None]] = []
    current: dict[str, str | None] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("Name:"):
            value = line.split(":", 1)[1].strip()
            if value.startswith("gfx"):
                current = {"gfx": value, "marketing_name": None, "compute_units": None}
                agents.append(current)
            else:
                current = None
        elif current is not None and line.startswith("Marketing Name:"):
            current["marketing_name"] = line.split(":", 1)[1].strip()
        elif current is not None and line.startswith("Compute Unit:"):
            current["compute_units"] = line.split(":", 1)[1].strip()
    return agents


def _detect_gpu_agents() -> list[dict[str, str | None]]:
    rocminfo = shutil.which("rocminfo")
    if not rocminfo:
        return []
    rc, output = _run([rocminfo])
    if rc != 0:
        return []
    return _parse_rocminfo_agents(output)


def _detect_arch() -> str | None:
    """The first detected GPU agent's ``gfxNNN`` id, or ``None``."""
    agents = _detect_gpu_agents()
    return agents[0]["gfx"] if agents else None


def _standard_rocm_lib_dirs() -> list[str]:
    roots = [os.environ.get("ROCM_PATH", "").strip(), "/opt/rocm"]
    return [f"{root}/lib" for root in roots if root]


def _find_att_decoder_dir() -> str | None:
    """Directory containing ``librocprof-trace-decoder.so``, or ``None``."""
    for env_var in _ATT_LIBRARY_PATH_ENV_VARS:
        candidate = os.environ.get(env_var, "").strip()
        if candidate and (Path(candidate) / _ATT_DECODER_LIB_NAME).is_file():
            return candidate
    for lib_dir in _standard_rocm_lib_dirs():
        if (Path(lib_dir) / _ATT_DECODER_LIB_NAME).is_file():
            return lib_dir
    return None


_ATTACH_LIB_NAME = "librocprofiler-sdk-attach.so"
_ATTACH_BG_THREAD_NAME = "rocp-bg-attach"
_ATTACH_PROBE_TIMEOUT_S = 5.0
_ATTACH_PROBE_POLL_S = 0.2


def _find_attach_lib() -> str | None:
    for lib_dir in _standard_rocm_lib_dirs():
        candidate = Path(lib_dir) / _ATTACH_LIB_NAME
        if candidate.is_file():
            return str(candidate)
    return None


def _probe_rocprofv3_attach() -> tuple[bool, str]:
    """Best-effort probe: can a process on this host expose the attach thread rocprofv3 needs?

    rocprofv3's ``--attach PID`` path requires the target process to have
    spun up a background thread named ``rocp-bg-attach`` (via
    ``ROCP_TOOL_ATTACH=1`` plus ``librocprofiler-sdk-attach.so`` on
    ``LD_PRELOAD``); that thread only exists when the host's
    ``librocprofiler-register`` was built with
    ``ROCPROFILER_REGISTER_BUILD_DEFAULT_ATTACHMENT=ON``. This spawns a
    short-lived helper process with that env set and polls
    ``/proc/<pid>/task/*/comm`` for the thread name, mirroring the exact
    recipe verified against a real MI210/ROCm 7.2.3 image: that image's
    build never produced the thread regardless of env, so the probe
    correctly reports unavailable there, and would report available on an
    image built with default attachment enabled.
    """
    attach_lib = _find_attach_lib()
    if attach_lib is None:
        return False, f"{_ATTACH_LIB_NAME} not found under $ROCM_PATH/lib or /opt/rocm/lib"
    env = {**os.environ, "ROCP_TOOL_ATTACH": "1", "LD_PRELOAD": attach_lib}
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return False, f"could not launch attach probe process: {exc}"
    try:
        found = _poll_for_thread_name(proc.pid, _ATTACH_BG_THREAD_NAME, _ATTACH_PROBE_TIMEOUT_S)
    finally:
        _kill_probe(proc)
    if found:
        return True, f"{_ATTACH_BG_THREAD_NAME} thread observed (attach lib: {attach_lib})"
    return False, (
        f"{_ATTACH_BG_THREAD_NAME} thread never appeared: this librocprofiler-register build was "
        "not compiled with ROCPROFILER_REGISTER_BUILD_DEFAULT_ATTACHMENT=ON (verified root cause "
        "on MI210/ROCm 7.2.3)"
    )


def _poll_for_thread_name(pid: int, name: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    task_dir = Path(f"/proc/{pid}/task")
    while time.monotonic() < deadline:
        if task_dir.is_dir():
            for entry in task_dir.iterdir():
                with contextlib.suppress(OSError):
                    if (entry / "comm").read_text().strip() == name:
                        return True
        time.sleep(_ATTACH_PROBE_POLL_S)
    return False


def _kill_probe(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=2.0)
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2.0)


def _capability_attach_line() -> str:
    available, detail = _probe_rocprofv3_attach()
    if available:
        return (
            f"rocprofv3 attach: probe succeeded ({detail}) -- VibeSys does not implement "
            "rocprofv3 --attach capture even where the underlying build supports it; every "
            "profile_* capture tool still launches its own target. For repeated windows on an "
            "already-running process, use the torch plugin's start_target + "
            "profile_ops(target=<id>) instead."
        )
    return (
        f"rocprofv3 attach: unavailable ({detail}) -- every profile_* capture tool must launch "
        "its own target for the capture's lifetime; passing target= to one returns this message "
        "instead of an rocprofv3 failure. For repeated windows on an already-running process, use "
        "the torch plugin's start_target + profile_ops(target=<id>) instead."
    )


def target_arg_unavailable_message() -> str:
    """The error text a rocprof capture tool returns when called with ``target=``.

    rocprofv3 attach cannot be exercised end to end regardless of what the
    capability probe reports (see ``_capability_attach_line``): every
    rocprofv3 ``profile_*`` capture tool always launches its own target.
    """
    _available, detail = _probe_rocprofv3_attach()
    return (
        f"error: target= is not supported by this capture tool ({detail}). Every rocprofv3 "
        "capture must launch its own target for the capture's lifetime (omit target=, pass "
        "command= instead). For repeated windows on an already-running process, use the torch "
        "plugin's start_target + profile_ops(target=<id>) instead."
    )


def import_torch_sibling(module_name: str) -> types.ModuleType | None:
    """Import *module_name* from the torch plugin's staged sibling directory, if present.

    Checkout layout: sibling directory ``torch`` (``resources/profilers/torch/``).
    Agent workspace: staged as ``torch_profiler`` (see the rocprof
    ``ProfilerDefinition``'s ``extra_support_kinds``). Returns ``None``
    (never raises) when neither is found, so callers can degrade with a
    clear message instead of crashing the MCP server.
    """
    for sibling_name in ("torch", "torch_profiler"):
        candidate_dir = _HERE.parent / sibling_name
        if (candidate_dir / f"{module_name}.py").is_file():
            if str(candidate_dir) not in sys.path:
                sys.path.insert(0, str(candidate_dir))
            return importlib.import_module(module_name)
    return None


# ---------------------------------------------------------------------------
# report/dirs argument resolution: accept a capture id OR an explicit path
# ---------------------------------------------------------------------------


def resolve_report_arg(value: str) -> str:
    """Accept a capture id OR an explicit path for a drill-down's ``report`` argument.

    Passes an existing file or directory straight through (preserving every
    analyzer's own path-based error messages), otherwise tries the capture
    store (a capture id from a ``profile_*`` tool or ``captures()``),
    falling back to *value* unchanged so the analyzer raises its own clear
    "not found" error rather than this function raising a different one.
    """
    if Path(value).exists():
        return value
    try:
        return str(capture_runtime.resolve(value))
    except FileNotFoundError:
        return value


def _is_counters_capture_dir(path: Path) -> bool:
    try:
        manifest = capture_runtime.load_manifest(path)
    except (FileNotFoundError, ValueError):
        return False
    return manifest.get("kind") == "counters"


def resolve_counter_dirs(dirs: list[str]) -> list[str]:
    """Expand any ``profile_counters`` capture id in *dirs* into its per-set pass directories.

    A ``profile_counters`` capture bundles one pass directory per requested
    counter set; passing its capture id here expands to every pass so the
    agent doesn't need to know the internal layout. An explicit path (to a
    single pass directory, or anything else) passes through unchanged.
    """
    resolved: list[str] = []
    for entry in dirs:
        try:
            candidate_dir = capture_runtime.resolve(entry)
        except FileNotFoundError:
            resolved.append(entry)
            continue
        if _is_counters_capture_dir(candidate_dir):
            manifest = capture_runtime.load_manifest(candidate_dir)
            resolved.extend(manifest.get("set_dirs", {}).values())
        else:
            resolved.append(str(candidate_dir))
    return resolved


def resolve_kernel_deep_workload_arg(value: str) -> str:
    """Accept a workload dir path OR a ``profile_kernel_deep`` capture id for ``compute_analyze``.

    A kernel_deep capture's id resolves to its top-level capture directory,
    not the rocprof-compute workload directory nested under it (recorded in
    the manifest's ``meta.workload_dir``); this looks that up.
    """
    if Path(value).is_dir():
        return value
    try:
        capture_dir = capture_runtime.resolve(value)
    except FileNotFoundError:
        return value
    try:
        manifest = capture_runtime.load_manifest(capture_dir)
    except (FileNotFoundError, ValueError):
        return value
    workload_dir = manifest.get("meta", {}).get("workload_dir")
    return workload_dir or value


def resolve_ops_trace_arg(value: str) -> str:
    """Accept a trace file path OR a ``profile_ops`` capture id for certify/gemm_shapes/roofline.

    Mirrors the torch plugin's own resolution: a ``profile_ops`` capture
    records the trace it picked as primary directly on its manifest
    (``primary_trace``, relative to the capture directory).
    """
    if Path(value).is_file():
        return value
    try:
        capture_dir = capture_runtime.resolve(value)
    except FileNotFoundError:
        return value
    try:
        manifest = capture_runtime.load_manifest(capture_dir)
    except (FileNotFoundError, ValueError):
        return value
    primary = manifest.get("primary_trace")
    return str(capture_dir / primary) if primary else value


def _capability_rocprofv3_line(
    rocprofv3_bin: str | None, version: tuple[int, int, int] | None
) -> str:
    if rocprofv3_bin and version is not None:
        return (
            f"rocprofv3: {rocprofv3_bin} (version {'.'.join(map(str, version))}) -- used by "
            "profile_timeline, profile_counters, profile_instructions."
        )
    if rocprofv3_bin:
        return (
            f"rocprofv3: {rocprofv3_bin} (version unparsable from --version output) -- used by "
            "profile_timeline, profile_counters, profile_instructions."
        )
    return (
        "rocprofv3: NOT FOUND on PATH or under $ROCM_PATH/bin -- profile_timeline, "
        "profile_counters, profile_instructions cannot run. Install ROCm's rocprofiler-sdk "
        "package, or add rocprofv3 to PATH."
    )


def _capability_gpu_agent_lines(agents: list[dict[str, str | None]]) -> list[str]:
    if not agents:
        return [
            "GPU agent: NOT DETECTED (rocminfo missing, or found no gfx agent) -- "
            "profile_counters/profile_kernel_deep need a detectable architecture; install "
            "rocminfo, or point $ROCM_PATH at a working ROCm install."
        ]
    return [
        f"GPU agent: {agent['gfx']} ({agent.get('marketing_name') or 'unknown SKU'}, "
        f"{agent.get('compute_units') or '?'} CUs) -- used by profile_counters (counter-set "
        "catalogue), profile_kernel_deep (occupancy model)."
        for agent in agents
    ]


def _capability_att_line(rocprofv3_bin: str | None, version: tuple[int, int, int] | None) -> str:
    decoder_dir = _find_att_decoder_dir()
    version_ok = version is not None and version >= _ATT_MIN_ROCPROFV3_VERSION
    if rocprofv3_bin and version_ok and decoder_dir:
        return (
            f"ATT (instruction-level trace): available (decoder at {decoder_dir}) -- used by "
            "profile_instructions."
        )
    reasons = []
    if not rocprofv3_bin or not version_ok:
        reasons.append("rocprofv3 >= 7.1 required (--att does not exist before that)")
    if not decoder_dir:
        reasons.append(
            "rocprof-trace-decoder library not found (set $VIBESYS_ROCPROF_ATT_LIBRARY_PATH or "
            "$ROCPROF_ATT_LIBRARY_PATH to its containing directory, or install it under "
            "$ROCM_PATH/lib)"
        )
    return (
        "ATT (instruction-level trace): NOT AVAILABLE -- "
        + "; ".join(reasons)
        + ". profile_instructions will fail."
    )


def _capability_compute_block() -> str:
    doctor_output = run_cli(compute.cmd_doctor)
    return (
        "rocprof-compute (kernel-internal Speed-of-Light/Roofline, used by profile_kernel_deep):\n"
        + textwrap.indent(doctor_output, "  ")
    )


def _capability_torch_line() -> str:
    ok, detail = _torch_available()
    if ok:
        return f"torch: available ({detail}) -- used by profile_ops."
    return f"torch: NOT AVAILABLE ({detail}) -- profile_ops cannot run."


def _capability_ops_line() -> str:
    ops_module = import_torch_sibling("capture_ops")
    if ops_module is None:
        return "torch capture_ops plugin: NOT STAGED alongside rocprof -- profile_ops cannot run."
    return "torch capture_ops plugin: available -- used by profile_ops."


def _capability_counter_sets_line(agents: list[dict[str, str | None]]) -> str | None:
    if not agents:
        return None
    arch = agents[0]["gfx"]
    if not arch:
        return None
    try:
        family = counters.normalize_arch(arch)
    except ValueError:
        return None
    catalogue = counters.COUNTER_SETS.get(family, {})
    if not catalogue:
        return (
            f"PMC counter sets for {arch}: none in the catalogue -- profile_counters will "
            "reject any set."
        )
    names = ", ".join(sorted(catalogue))
    return f"PMC counter sets for {arch}: {names} -- used by profile_counters (sets=[...])."


def profiling_capabilities() -> str:
    """Report what this host actually supports, and which tool each line gates.

    Call this first, every round: each line names the ``profile_*`` tool it
    gates, or why it's unavailable and how to fix it -- so a missing
    capability is ruled out up front instead of discovered by a failed
    capture. Covers rocprofv3 (path + version), GPU agents (rocminfo), ATT
    availability, rocprof-compute (via its own doctor checks), torch, the
    torch ``profile_ops`` delegate, the capture store location, and the PMC
    counter-set catalogue for the detected architecture.
    """
    rocprofv3_bin = compute.find_rocprofv3_bin()
    version = _rocprofv3_version(rocprofv3_bin) if rocprofv3_bin else None
    agents = _detect_gpu_agents()

    sections = [
        _capability_rocprofv3_line(rocprofv3_bin, version),
        *_capability_gpu_agent_lines(agents),
        _capability_att_line(rocprofv3_bin, version),
        _capability_attach_line()
        if rocprofv3_bin
        else "rocprofv3 attach: unavailable (rocprofv3 itself not found; see the rocprofv3 line "
        "above).",
        _capability_compute_block(),
        _capability_torch_line(),
        _capability_ops_line(),
        f"capture store: {capture_runtime.profiles_root()} (override with $VIBESYS_PROFILE_DIR).",
    ]
    counter_line = _capability_counter_sets_line(agents)
    if counter_line:
        sections.append(counter_line)
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# profile_timeline: rocprofv3 system trace
# ---------------------------------------------------------------------------


def profile_timeline(  # noqa: PLR0913  # LW-910049; this function's parameters mirror an external tool's CLI/API surface and are not grouped further
    lifecycle: capture_runtime.Lifecycle,
    *,
    hip_api: bool = False,
    kernel_include: str | None = None,
    collection_delay_s: float | None = None,
    collection_duration_s: float | None = None,
    target: str | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """Capture a whole-run rocprofv3 system trace: host/device timeline.

    Runs ``rocprofv3 --kernel-trace --memory-copy-trace --stats`` (plus
    ``--hip-runtime-trace`` when ``hip_api=True``, and ``--collection-period``
    when both ``collection_delay_s``/``collection_duration_s`` are given, to
    skip a warmup window) through the shared capture lifecycle, then runs
    ``host_idle`` and ``summary`` against the result automatically. A server
    capture (``load_command`` given) can include a long startup (weight
    load, warmup, KV init) ahead of the actual benchmarked load; the
    auto-summary defaults to the recorded load-phase window rather than the
    whole run, same as every drill-down below unless you pass
    ``window='all'``. Next: ``kernels``, ``families``, ``idle_gaps``,
    ``cpu_overhead``, ``memory``, ``graphs``, ``host_idle``, ``summary``, or
    ``compare`` against another timeline capture.

    Serialized against every other GPU-using capture in this process via
    ``capture_runtime.exclusive_capture``: raises ``CaptureBusyError`` (not
    caught here -- the MCP boundary formats it) if another capture is
    already running. ``cancel_event``, when given, is forwarded to
    ``capture_runtime.run_capture`` so a caller can stop this capture from
    another thread (see ``resources/profilers/_common/mcp_async.py``).

    ``target`` is not supported by this tool (rocprofv3 attach cannot be
    exercised end to end regardless of host); passing it returns a clear
    error naming the fix instead of an obscure rocprofv3 failure -- see
    ``target_arg_unavailable_message``.
    """
    if target is not None:
        return target_arg_unavailable_message()
    if (collection_delay_s is None) != (collection_duration_s is None):
        raise ValueError(  # noqa: TRY003  # LW-910050; this is a boundary error that deliberately embeds the offending value for the operator to act on
            "collection_delay_s and collection_duration_s must both be given, or neither "
            "(rocprofv3's --collection-period needs a start_delay:collection_time:repeat triplet)"
        )
    capture_id, out_dir = capture_runtime.new_capture("timeline")
    prefix = ["rocprofv3", "--kernel-trace"]
    if hip_api:
        prefix.append("--hip-runtime-trace")
    prefix += ["--memory-copy-trace", "--stats", "--output-format", "csv"]
    if kernel_include:
        prefix += ["--kernel-include-regex", kernel_include]
    if collection_delay_s is not None and collection_duration_s is not None:
        prefix += ["--collection-period", f"{collection_delay_s:g}:{collection_duration_s:g}:1"]
    prefix += ["-d", str(out_dir), "--"]

    with capture_runtime.exclusive_capture("timeline", capture_id):
        result = capture_runtime.run_capture(
            prefix,
            lifecycle,
            kind="timeline",
            out_dir=out_dir,
            meta={"hip_api": hip_api, "kernel_include": kernel_include},
            cancel_event=cancel_event,
        )
    return _format_timeline_result(result)


# Keyword heuristics for suggesting a profile_counters drill-down from a
# timeline's top kernel by name only (best-effort; a kernel matching neither
# gets no set suggestion, just the drill-down pointer). GEMM-family kernels
# are compute-bound on the matrix core and HBM feed; attention-family
# kernels are compute-bound on the matrix core and L2/KV-cache reuse -- see
# counters.py's per-arch catalogues for what "mfma"/"hbm"/"l2" cover.
_GEMM_KERNEL_NAME_RE = re.compile(
    r"gemm|matmul|hipblaslt|rocblas|cutlass|\bmm\b|wmma", re.IGNORECASE
)
_ATTENTION_KERNEL_NAME_RE = re.compile(
    r"attn|attention|flash|fmha|paged.?kv|decode.?kv", re.IGNORECASE
)
_COUNTER_SETS_BY_KERNEL_FAMILY = {
    "gemm": ["mfma", "hbm"],
    "attention": ["mfma", "l2"],
}


def _suggest_counter_drilldown(out_dir: Path) -> str | None:
    """Suggest a concrete ``profile_counters`` call for the timeline's top kernel.

    Uses the same load-phase window as the rest of this auto-summary (see
    ``kernel_time_totals``'s default), so the suggestion targets whatever
    dominates steady-state GPU time, not one-time startup kernels. Returns
    ``None`` when there's no kernel data to suggest from.
    """
    try:
        totals = analyze_rocprof.kernel_time_totals(str(out_dir))
    except (OSError, ValueError):
        return None
    if not totals:
        return None
    top_kernel = max(totals, key=lambda name: totals[name])
    if _GEMM_KERNEL_NAME_RE.search(top_kernel):
        family = "gemm"
    elif _ATTENTION_KERNEL_NAME_RE.search(top_kernel):
        family = "attention"
    else:
        family = None
    sets = _COUNTER_SETS_BY_KERNEL_FAMILY.get(family, ["mfma"]) if family else ["mfma"]
    sets_repr = ", ".join(repr(s) for s in sets)
    return (
        f"Top kernel by GPU time: {top_kernel!r}. Suggested drill-down: "
        f"profile_counters(sets=[{sets_repr}], kernel={re.escape(top_kernel)!r})."
    )


def _format_timeline_result(result: capture_runtime.CaptureResult) -> str:
    lines = [capture_runtime.format_result(result)]
    if result.status is not capture_runtime.CaptureStatus.OK:
        # A non-OK status means the *overall* lifecycle didn't exit cleanly
        # (the target needed escalation, the load command failed, etc.), but
        # rocprofv3 only needs its own stop_signal delivered to flush a
        # trace: a serving-engine capture can have rocprofv3 finish "output
        # generation"/"tool finalization" within seconds of stop_signal,
        # while some other thread in the same process group (observed with
        # a real multi-threaded serving engine's API-server process under a
        # graceful SIGINT; see the profiling-serving-engines skill
        # references for the engine-specific detail) keeps the process
        # alive until this lifecycle gives up and escalates -- the trace on
        # disk is real and complete regardless. Attempt the analysis
        # unconditionally rather than withholding it: every analyzer
        # function already degrades cleanly ("no kernel data found") when
        # nothing was actually written.
        lines.append(
            "\nCapture did not complete cleanly (see the log tail above); analyzing whatever "
            "rocprofv3 output exists anyway, since rocprofv3 can flush a complete trace before "
            "an unrelated hang forces this lifecycle to escalate. Treat the findings below as "
            "provisional until corroborated."
        )
    lines.append("")
    lines.append(run_cli(analyze_rocprof.cmd_host_idle, report=str(result.out_dir)))
    lines.append("")
    lines.append(run_cli(analyze_rocprof.cmd_summary, report=str(result.out_dir), top=15))
    suggestion = _suggest_counter_drilldown(result.out_dir)
    if suggestion:
        lines.append(f"\n{suggestion}")
    lines.append(
        f"\nNext: kernels/families/idle_gaps/cpu_overhead/memory/graphs(report={result.capture_id!r}), "
        "or compare(a, b) against another timeline capture."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# profile_counters: pack requested counter sets into as few rocprofv3 --pmc
# passes as possible, falling back to one pass per set on rejection
# ---------------------------------------------------------------------------


def _worst_status(
    statuses: Iterable[capture_runtime.CaptureStatus],
) -> capture_runtime.CaptureStatus:
    return max(statuses, key=lambda status: _STATUS_SEVERITY[status])


@dataclasses.dataclass(frozen=True)
class _PassRun:
    """One executed rocprofv3 --pmc invocation: the set(s) it covered and its outcome."""

    group: list[str]
    result: capture_runtime.CaptureResult


def _run_counter_pass(  # noqa: PLR0913  # LW-910051; this function's parameters mirror an external tool's CLI/API surface and are not grouped further
    *,
    lifecycle: capture_runtime.Lifecycle,
    out_dir: Path,
    group: list[str],
    catalogue: dict[str, counters.CounterSet],
    kernel: str | None,
    arch: str,
    cancel_event: threading.Event | None,
) -> capture_runtime.CaptureResult:
    pmc_counters = counters.pass_counters(catalogue, group)
    prefix = ["rocprofv3", "--pmc", *pmc_counters, "--output-format", "csv"]
    if kernel:
        prefix += ["--kernel-include-regex", kernel]
    prefix += ["-d", str(out_dir), "--"]
    return capture_runtime.run_capture(
        prefix,
        lifecycle,
        kind="counters_pass",
        out_dir=out_dir,
        meta={"sets": group, "arch": arch},
        cancel_event=cancel_event,
    )


def _run_planned_group(  # noqa: PLR0913  # LW-910052; this function's parameters mirror an external tool's CLI/API surface and are not grouped further
    *,
    lifecycle: capture_runtime.Lifecycle,
    out_dir: Path,
    group: list[str],
    catalogue: dict[str, counters.CounterSet],
    kernel: str | None,
    arch: str,
    cancel_event: threading.Event | None,
) -> list[_PassRun]:
    """Run one planned pass group, falling back to one pass per set if rocprofv3 rejects it.

    A singleton group (one set) is just run directly -- there is nothing to
    fall back from. A multi-set group that fails with log text matching
    ``counters.looks_like_packed_pass_rejection`` is re-run as one pass per
    set instead of being reported as a single failed packed pass; any other
    failure (a real target/workload failure, not a packing rejection) is
    reported as-is with no retry, since re-running it split would not fix it
    and would only double the cost. A pass that ends CANCELLED (client
    aborted the tool call) is never retried either, for the same reason.
    """
    sub_dir = out_dir / "+".join(group)
    result = _run_counter_pass(
        lifecycle=lifecycle,
        out_dir=sub_dir,
        group=group,
        catalogue=catalogue,
        kernel=kernel,
        arch=arch,
        cancel_event=cancel_event,
    )
    if (
        len(group) == 1
        or result.status is capture_runtime.CaptureStatus.CANCELLED
        or not counters.looks_like_packed_pass_rejection(
            status_ok=result.status is capture_runtime.CaptureStatus.OK,
            log_tail=result.target_log_tail,
        )
    ):
        return [_PassRun(group=group, result=result)]

    return [
        _PassRun(
            group=[set_name],
            result=_run_counter_pass(
                lifecycle=lifecycle,
                out_dir=out_dir / set_name,
                group=[set_name],
                catalogue=catalogue,
                kernel=kernel,
                arch=arch,
                cancel_event=cancel_event,
            ),
        )
        for set_name in group
    ]


def profile_counters(
    lifecycle: capture_runtime.Lifecycle,
    *,
    sets: list[str],
    kernel: str | None = None,
    target: str | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """Capture PMC hardware counters for one or more named counter sets.

    Packs the requested sets into as few rocprofv3 --pmc passes as a
    conservative per-hardware-block model allows (see
    ``counters.plan_passes``): real MI210 validation packed mfma+hbm and
    mfma+l2 into one pass each. **Each pass, not each requested set, re-runs
    the profiled workload in full** -- the returned text and this capture's
    manifest report how many passes were planned vs. actually run. A pass
    rocprofv3 rejects (log text naming a counters-don't-fit failure) falls
    back automatically to one pass per set in that group, raising the run
    count above the plan for that capture only. Requires a detectable GPU
    architecture (see ``profiling_capabilities``); target one
    already-identified hot kernel via ``kernel``, not a whole run. After
    every pass completes, runs ``counter_triage`` automatically. Next:
    ``counter_report``, ``counter_triage``, or ``compare`` against another
    counters capture.

    Serialized against every other GPU-using capture in this process via
    ``capture_runtime.exclusive_capture``: raises ``CaptureBusyError`` (not
    caught here -- the MCP boundary formats it) if another capture is
    already running. ``cancel_event``, when given, stops the in-flight pass
    and skips any not-yet-started passes rather than continuing to run the
    plan out (see ``resources/profilers/_common/mcp_async.py``).

    ``target`` is not supported by this tool; see ``profile_timeline``'s
    docstring for why (rocprofv3 attach unavailable, use the torch plugin's
    warm-target profile_ops instead).
    """
    if target is not None:
        return target_arg_unavailable_message()
    if not sets:
        raise ValueError("sets must name at least one counter set (see profiling_capabilities)")  # noqa: TRY003  # LW-910053; this is a boundary error that deliberately embeds the offending value for the operator to act on
    arch = _detect_arch()
    if arch is None:
        raise ValueError(  # noqa: TRY003  # LW-910054; this is a boundary error that deliberately embeds the offending value for the operator to act on
            "could not detect a GPU architecture (rocminfo missing, or found no gfx agent); "
            "check profiling_capabilities"
        )
    family = counters.normalize_arch(arch)
    catalogue = counters.COUNTER_SETS.get(family, {})
    unknown = [s for s in sets if s not in catalogue]
    if unknown:
        known = ", ".join(sorted(catalogue))
        raise ValueError(f"unknown counter set(s) {unknown} for {family}; known sets: {known}")  # noqa: TRY003  # LW-910055; this is a boundary error that deliberately embeds the offending value for the operator to act on

    capture_id, out_dir = capture_runtime.new_capture("counters")
    with capture_runtime.exclusive_capture("counters", capture_id):
        planned_groups = counters.plan_passes(catalogue, sets)
        pass_runs: list[_PassRun] = []
        set_dirs: dict[str, Path] = {}
        for group in planned_groups:
            if cancel_event is not None and cancel_event.is_set():
                break
            for pass_run in _run_planned_group(
                lifecycle=lifecycle,
                out_dir=out_dir,
                group=group,
                catalogue=catalogue,
                kernel=kernel,
                arch=family,
                cancel_event=cancel_event,
            ):
                pass_runs.append(pass_run)
                for set_name in pass_run.group:
                    set_dirs[set_name] = pass_run.result.out_dir

    overall_status = (
        _worst_status(run.result.status for run in pass_runs)
        if pass_runs
        else capture_runtime.CaptureStatus.CANCELLED
    )
    passes_planned = len(planned_groups)
    passes_run = len(pass_runs)
    capture_runtime.write_manifest(
        out_dir,
        {
            "capture_id": capture_id,
            "kind": "counters",
            "status": overall_status.value,
            "arch": family,
            "kernel": kernel,
            "sets": sets,
            "set_dirs": {name: str(path) for name, path in set_dirs.items()},
            "passes_planned": passes_planned,
            "passes_run": passes_run,
        },
    )
    return _format_counters_result(
        capture_id=capture_id,
        sets=sets,
        pass_runs=pass_runs,
        overall_status=overall_status,
        set_dirs=set_dirs,
        arch=family,
        kernel=kernel,
        out_dir=out_dir,
        passes_planned=passes_planned,
        passes_run=passes_run,
    )


def _format_counters_result(  # noqa: PLR0913  # LW-910056; this function's parameters mirror an external tool's CLI/API surface and are not grouped further
    *,
    capture_id: str,
    sets: list[str],
    pass_runs: list[_PassRun],
    overall_status: capture_runtime.CaptureStatus,
    set_dirs: dict[str, Path],
    arch: str,
    kernel: str | None,
    out_dir: Path,
    passes_planned: int,
    passes_run: int,
) -> str:
    fallback_note = (
        " (a rejected packed pass fell back to more passes)" if passes_run > passes_planned else ""
    )
    lines = [
        f"capture {capture_id} (counters): {overall_status.value}  "
        f"({passes_run} pass(es) run, {passes_planned} planned, {len(sets)} set(s) requested)"
        f"{fallback_note}"
    ]
    lines.extend(
        f"  pass {'+'.join(run.group)}: {run.result.status.value} "
        f"({run.result.timings.get('duration_s', 0.0):.2f}s)"
        for run in pass_runs
    )
    if overall_status is not capture_runtime.CaptureStatus.OK:
        lines.append(f"\nAt least one pass did not complete cleanly; see each pass under {out_dir}")
        return "\n".join(lines)
    dirs = counters.dedupe_preserve_order(str(d) for d in set_dirs.values())
    lines.append("")
    lines.append(
        run_cli(
            counters.cmd_triage,
            dirs=dirs,
            arch=arch,
            kernel=kernel,
            top=15,
        )
    )
    lines.append(
        f"\nNext: counter_report(dirs={dirs}), or compare(a, b) against another counters capture."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# profile_kernel_deep: rocprof-compute profile + analyze
# ---------------------------------------------------------------------------


def _workload_has_kernel_data(workload_dir: Path) -> bool:
    for name in _WORKLOAD_KERNEL_CSV_NAMES:
        path = workload_dir / name
        if not path.is_file():
            continue
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        return len(rows) > 1
    return False


def profile_kernel_deep(
    lifecycle: capture_runtime.Lifecycle,
    *,
    kernel: str,
    dispatch: int | None = None,
    target: str | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """Capture full Speed-of-Light + roofline for one targeted kernel (rocprof-compute).

    Runs ``rocprof-compute profile`` through the shared capture lifecycle,
    then ``rocprof-compute analyze``. ``kernel`` matches a literal substring
    of the real kernel name, not a friendly alias (torch GEMMs dispatch as
    Tensile kernels like ``Cijk_Ailk_Bljk_...``, not anything containing
    "gemm"): list real names with ``profile_timeline`` + ``kernels`` first.
    Costs one full counter-collection sweep (minutes, not seconds). If
    rocprof-compute isn't usable on this host, returns a clear error
    pointing at ``profiling_capabilities`` instead of attempting a capture.
    Next: ``compute_analyze``, or ``compare`` against another kernel_deep
    capture.

    Serialized against every other GPU-using capture in this process via
    ``capture_runtime.exclusive_capture``: raises ``CaptureBusyError`` (not
    caught here -- the MCP boundary formats it) if another capture is
    already running. ``cancel_event``, when given, is forwarded to
    ``capture_runtime.run_capture`` so a caller can stop this capture from
    another thread (see ``resources/profilers/_common/mcp_async.py``).

    ``target`` is not supported by this tool; see ``profile_timeline``'s
    docstring for why.
    """
    if target is not None:
        return target_arg_unavailable_message()
    if not kernel:
        raise ValueError("kernel is required (a literal substring of the real kernel name)")  # noqa: TRY003  # LW-910057; this is a boundary error that deliberately embeds the offending value for the operator to act on
    rocprof_bin = compute.find_rocprof_compute_bin()
    python = compute.find_deps_python(rocprof_bin) if rocprof_bin else None
    if not rocprof_bin or not python:
        return (
            "error: rocprof-compute is not usable on this host (no binary, or no interpreter "
            "satisfies its dependency gate); see profiling_capabilities for the fix."
        )

    capture_id, out_dir = capture_runtime.new_capture("kernel_deep")
    workload_dir = out_dir / "workloads" / capture_id
    prefix = [
        python,
        rocprof_bin,
        "profile",
        "-n",
        capture_id,
        "-p",
        str(workload_dir),
        "-k",
        kernel,
    ]
    if dispatch is not None:
        prefix += ["--dispatch", str(dispatch)]
    prefix.append("--")

    with capture_runtime.exclusive_capture("kernel_deep", capture_id):
        result = capture_runtime.run_capture(
            prefix,
            lifecycle,
            kind="kernel_deep",
            out_dir=out_dir,
            meta={"kernel": kernel, "dispatch": dispatch, "workload_dir": str(workload_dir)},
            cancel_event=cancel_event,
        )
    return _format_kernel_deep_result(
        result, kernel=kernel, dispatch=dispatch, workload_dir=workload_dir
    )


def _format_kernel_deep_result(
    result: capture_runtime.CaptureResult, *, kernel: str, dispatch: int | None, workload_dir: Path
) -> str:
    lines = [capture_runtime.format_result(result)]
    if result.status is not capture_runtime.CaptureStatus.OK:
        lines.append("\nCapture did not complete cleanly; see the log tail above.")
        return "\n".join(lines)
    if not _workload_has_kernel_data(workload_dir):
        dispatch_note = f", dispatch={dispatch}" if dispatch is not None else ""
        lines.append(
            f"\nProfile completed but matched no kernel dispatches for kernel={kernel!r}"
            f"{dispatch_note}. torch GEMMs dispatch through hipBLASLt/Tensile kernels (e.g. "
            "'Cijk_Ailk_Bljk_...'), not a literal 'gemm' substring; list real kernel names with "
            "profile_timeline + kernels first."
        )
        return "\n".join(lines)
    lines.append("")
    lines.append(
        run_cli(
            compute.cmd_analyze,
            workload_dir=str(workload_dir),
            blocks=",".join(compute.DEFAULT_ANALYZE_BLOCKS),
            max_stat=compute.DEFAULT_MAX_STAT,
            kernel="",
            timeout=compute.DEFAULT_ANALYZE_TIMEOUT,
        )
    )
    lines.append(
        f"\nNext: compute_analyze(workload_dir={str(workload_dir)!r}), or compare(a, b) against "
        "another kernel_deep capture."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# profile_instructions: rocprofv3 ATT capture + decode + hotspots
# ---------------------------------------------------------------------------


def _find_att_dispatch_dir(out_dir: Path) -> Path | None:
    matches = sorted(out_dir.rglob("ui_output_agent_*_dispatch_*/code.json"))
    return matches[0].parent if matches else None


def profile_instructions(  # noqa: PLR0913  # LW-910058; this function's parameters mirror an external tool's CLI/API surface and are not grouped further
    lifecycle: capture_runtime.Lifecycle,
    *,
    kernel: str,
    target_cu: int = att.DEFAULT_TARGET_CU,
    buffer_bytes: int = att.DEFAULT_BUFFER_SIZE,
    target: str | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """Capture per-instruction stalls inside one kernel, one compute unit (ATT).

    Runs ``rocprofv3 --att`` through the shared capture lifecycle, then
    decodes and summarizes stall hotspots. Requires rocprofv3 >= 7.1 and the
    separate rocprof-trace-decoder library (see ``profiling_capabilities``);
    costs the most overhead of any capture tool here -- only reach for it
    after counters already point at a specific stall class. Next:
    ``att_hotspots``, or ``compare`` against another instructions capture.

    Serialized against every other GPU-using capture in this process via
    ``capture_runtime.exclusive_capture``: raises ``CaptureBusyError`` (not
    caught here -- the MCP boundary formats it) if another capture is
    already running. ``cancel_event``, when given, is forwarded to
    ``capture_runtime.run_capture`` so a caller can stop this capture from
    another thread (see ``resources/profilers/_common/mcp_async.py``).

    ``target`` is not supported by this tool; see ``profile_timeline``'s
    docstring for why.
    """
    if target is not None:
        return target_arg_unavailable_message()
    if not kernel:
        raise ValueError("kernel is required (a --kernel-include-regex value)")  # noqa: TRY003  # LW-910059; this is a boundary error that deliberately embeds the offending value for the operator to act on
    decoder_dir = _find_att_decoder_dir()
    if decoder_dir is None:
        return (
            "error: no rocprof-trace-decoder library found (set $VIBESYS_ROCPROF_ATT_LIBRARY_PATH "
            "or $ROCPROF_ATT_LIBRARY_PATH, or install it under $ROCM_PATH/lib); see "
            "profiling_capabilities."
        )
    rocprofv3_bin = compute.find_rocprofv3_bin()
    if rocprofv3_bin is None:
        return "error: rocprofv3 not found; see profiling_capabilities."
    version = _rocprofv3_version(rocprofv3_bin)
    if version is None or version < _ATT_MIN_ROCPROFV3_VERSION:
        found = ".".join(map(str, version)) if version else "unknown"
        needed = ".".join(map(str, _ATT_MIN_ROCPROFV3_VERSION))
        return f"error: rocprofv3 {found} does not support --att (needs >= {needed}); see profiling_capabilities."

    capture_id, out_dir = capture_runtime.new_capture("instructions")
    prefix = [
        "rocprofv3",
        "--att",
        "--att-target-cu",
        str(target_cu),
        "--att-simd-select",
        att.DEFAULT_SIMD_SELECT,
        "--att-shader-engine-mask",
        att.DEFAULT_SE_MASK,
        "--att-buffer-size",
        str(buffer_bytes),
        "--att-library-path",
        decoder_dir,
        "--kernel-include-regex",
        kernel,
        "-d",
        str(out_dir),
        "--output-format",
        "csv",
        "json",
        "--",
    ]
    with capture_runtime.exclusive_capture("instructions", capture_id):
        result = capture_runtime.run_capture(
            prefix,
            lifecycle,
            kind="instructions",
            out_dir=out_dir,
            meta={"kernel": kernel, "target_cu": target_cu, "buffer_bytes": buffer_bytes},
            cancel_event=cancel_event,
        )
    return _format_instructions_result(result)


def _format_instructions_result(result: capture_runtime.CaptureResult) -> str:
    lines = [capture_runtime.format_result(result)]
    if result.status is not capture_runtime.CaptureStatus.OK:
        lines.append("\nCapture did not complete cleanly; see the log tail above.")
        return "\n".join(lines)
    dispatch_dir = _find_att_dispatch_dir(result.out_dir)
    if dispatch_dir is None:
        lines.append(
            "\nNo decoded ui_output_agent_*_dispatch_* directory was produced: the kernel matched "
            "no dispatches, or matched dispatches the decoder could not resolve (e.g. degenerate "
            "fill kernels). Confirm the kernel name with profile_timeline + kernels first."
        )
        return "\n".join(lines)
    lines.append("")
    lines.append(run_cli(att.cmd_hotspots, dispatch_dir=str(dispatch_dir), top=15))
    lines.append(
        f"\nNext: att_hotspots(dispatch_dir={str(dispatch_dir)!r}), or compare(a, b) against "
        "another instructions capture."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# profile_ops: delegate to the torch plugin's capture_ops
# ---------------------------------------------------------------------------


def profile_ops(  # noqa: PLR0913  # LW-910060; this function's parameters mirror an external tool's CLI/API surface and are not grouped further
    *,
    command: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    ready_command: str | None = None,
    ready_timeout_s: float = 600.0,
    load_command: str | None = None,
    stop_signal: str = "SIGINT",
    grace_s: float = 120.0,
    timeout_s: float = 1800.0,
    delay_s: float = 0.0,
    duration_s: float | None = None,
    record_shapes: bool = True,
    inject: bool = True,
    setup_command: str | None = None,
    target: str | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """Capture a torch.profiler trace of an offline script, microbenchmark, or warm target.

    Thin delegation to the torch plugin's ``capture_ops.profile_ops``,
    staged alongside rocprof (see the rocprof ``ProfilerDefinition``'s
    ``extra_support_kinds``). Its lifecycle args and defaults match that
    module directly (grace/timeout are generous: the in-process torch trace
    write can itself take a while). Pass ``target`` (from ``start_target``)
    instead of ``command`` to window an already-running warm target; see
    that module's docstring. Returns a clear error, rather than raising, if
    that module isn't importable. Next: ``certify``, ``gemm_shapes``,
    ``roofline``, or ``summary`` (dispatches to the torch analyzer).

    Set ``inject=False`` when ``command`` already opens its own
    ``torch.profiler.profile()`` session internally (e.g. a serving engine's
    native ``profiler_config`` + ``start_profile()``/``stop_profile()``
    hooks): two independent profiler sessions in one process crash the
    CUPTI/roctracer/kineto backend outright (a SIGSEGV, not a catchable
    Python error -- this is what produced a real ``target_rc=139`` with an
    empty trace on MI210). With ``inject=False`` this tool never arms its
    own signal-based session (no ``VIBESYS_TORCH_PROFILE``, no PYTHONPATH
    injection of ``sitecustomize.py``); it still sets
    ``VIBESYS_TORCH_PROFILE_OUT_DIR`` so the command's own profiler can
    write its trace where this tool's existing discovery/analysis pipeline
    will find it.
    """
    ops_module = import_torch_sibling("capture_ops")
    if ops_module is None:
        return (
            "error: the torch profiler plugin's capture_ops module is not staged alongside "
            "rocprof (expected a 'torch' or 'torch_profiler' sibling directory with "
            "capture_ops.py); profile_ops is unavailable."
        )
    profile_ops_fn = getattr(ops_module, "profile_ops", None)
    if profile_ops_fn is None:
        return "error: torch capture_ops module has no profile_ops() function."
    try:
        return profile_ops_fn(
            command=command,
            cwd=cwd,
            env=env,
            ready_command=ready_command,
            ready_timeout_s=ready_timeout_s,
            load_command=load_command,
            stop_signal=stop_signal,
            grace_s=grace_s,
            timeout_s=timeout_s,
            delay_s=delay_s,
            duration_s=duration_s,
            record_shapes=record_shapes,
            inject=inject,
            setup_command=setup_command,
            target=target,
            cancel_event=cancel_event,
        )
    except TypeError as exc:
        return f"error: torch capture_ops.profile_ops() signature mismatch: {exc}"


# ---------------------------------------------------------------------------
# start_target / stop_target / targets: warm-target lifecycle (shared with
# the torch plugin -- see capture_ops.start_target). rocprof's own
# rocprofv3-based captures cannot use a warm target (see
# target_arg_unavailable_message above); this exists so an agent using only
# the rocprof MCP server can still start/stop a target for the delegated
# torch profile_ops(target=...) above, without needing a second MCP server.
# ---------------------------------------------------------------------------


def start_target(  # noqa: PLR0913  # LW-910061; this function's parameters mirror an external tool's CLI/API surface and are not grouped further
    command: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    setup_command: str | None = None,
    ready_command: str | None = None,
    ready_timeout_s: float = 60.0,
    stop_signal: str = "SIGINT",
    grace_s: float = 10.0,
    timeout_s: float = 300.0,
) -> str:
    """Launch a reusable, warm target process for the torch plugin's profile_ops(target=...).

    Delegates to the torch plugin's ``capture_ops.start_target`` (the same
    function ``torch/server.py``'s own ``start_target`` tool calls -- one
    shared implementation), which arms the process for repeated torch
    signal-window captures. Returns a clear error, rather than raising, if
    that module isn't importable.
    """
    ops_module = import_torch_sibling("capture_ops")
    if ops_module is None:
        return (
            "error: the torch profiler plugin's capture_ops module is not staged alongside "
            "rocprof (expected a 'torch' or 'torch_profiler' sibling directory with "
            "capture_ops.py); start_target is unavailable."
        )
    start_target_fn = getattr(ops_module, "start_target", None)
    if start_target_fn is None:
        return "error: torch capture_ops module has no start_target() function."
    try:
        return start_target_fn(
            command,
            cwd=cwd,
            env=env,
            setup_command=setup_command,
            ready_command=ready_command,
            ready_timeout_s=ready_timeout_s,
            stop_signal=stop_signal,
            grace_s=grace_s,
            timeout_s=timeout_s,
        )
    except RuntimeError as exc:
        return f"error: {exc}"


# ---------------------------------------------------------------------------
# captures: list what's been taken this session
# ---------------------------------------------------------------------------


def _command_head(capture_dir: Path, *, max_chars: int = 60) -> str:
    try:
        manifest = capture_runtime.load_manifest(capture_dir)
    except (FileNotFoundError, ValueError):
        return ""
    lifecycle = manifest.get("lifecycle")
    command = lifecycle.get("command") if isinstance(lifecycle, dict) else None
    if not isinstance(command, str):
        return ""
    return command if len(command) <= max_chars else command[: max_chars - 1] + "…"


def captures(limit: int = 10) -> str:
    """List recent captures: id, kind, status, and the profiled command's head.

    Args:
        limit: Maximum number of captures to show, newest first (default 10).
    """
    summaries = capture_runtime.list_captures(limit=limit)
    if not summaries:
        return f"(no captures under {capture_runtime.profiles_root()})"
    lines = [f"{'capture_id':<32} {'kind':<14} {'status':<20} command"]
    lines.extend(
        f"{entry.capture_id:<32} {entry.kind or '?':<14} {entry.status or '?':<20} "
        f"{_command_head(entry.dir)}"
        for entry in summaries
    )
    return "\n".join(lines)


def stop_target(target: str) -> str:
    """Stop a warm target started with start_target: stop_signal -> grace -> escalate."""
    try:
        capture_runtime.stop_target(target)
    except KeyError as exc:
        return f"error: {exc}"
    return f"stopped target {target}"


def targets() -> str:
    """List targets currently running in this server process (from start_target)."""
    return capture_runtime.format_targets(capture_runtime.list_targets())


# ---------------------------------------------------------------------------
# summary: dispatch on manifest kind
# ---------------------------------------------------------------------------


def _counters_triage_from_manifest(manifest: dict[str, Any]) -> str:
    set_dirs = manifest.get("set_dirs")
    arch = manifest.get("arch")
    if not isinstance(set_dirs, dict) or not set_dirs or not arch:
        return "error: counters manifest is missing set_dirs/arch"
    return run_cli(
        counters.cmd_triage,
        dirs=list(set_dirs.values()),
        arch=arch,
        kernel=manifest.get("kernel"),
        top=15,
    )


def _kernel_deep_analyze_from_manifest(manifest: dict[str, Any]) -> str:
    workload_dir = manifest.get("meta", {}).get("workload_dir")
    if not workload_dir:
        return "error: kernel_deep manifest is missing meta.workload_dir"
    return run_cli(
        compute.cmd_analyze,
        workload_dir=workload_dir,
        blocks=",".join(compute.DEFAULT_ANALYZE_BLOCKS),
        max_stat=compute.DEFAULT_MAX_STAT,
        kernel="",
        timeout=compute.DEFAULT_ANALYZE_TIMEOUT,
    )


def _ops_summary(capture_dir: Path, manifest: dict[str, Any]) -> str:
    """Summarize an ``ops`` capture via the torch analyzer, using its ``capture_ops``-recorded primary trace."""
    analyze_torch_profile = import_torch_sibling("analyze_torch_profile")
    if analyze_torch_profile is None:
        return "error: the torch analyzer module is not staged alongside rocprof."
    # capture_ops.profile_ops records the trace it picked as "primary_trace"
    # (relative to the capture dir) directly on the manifest; fall back to a
    # glob in case an older/foreign ops capture didn't.
    primary_trace = manifest.get("primary_trace")
    if primary_trace:
        report: str | None = str(capture_dir / primary_trace)
    else:
        candidates = sorted(capture_dir.rglob("*.pt.trace.json*")) + sorted(
            capture_dir.rglob("prof.json")
        )
        report = str(candidates[0]) if candidates else None
    if not report:
        return "error: could not locate a torch trace/report file under this ops capture"
    return run_cli(analyze_torch_profile.cmd_summary, report=report, top=15)


def _summary_timeline(capture_dir: Path, _manifest: dict[str, Any]) -> str:
    return run_cli(analyze_rocprof.cmd_summary, report=str(capture_dir), top=15)


def _summary_counters(_capture_dir: Path, manifest: dict[str, Any]) -> str:
    return _counters_triage_from_manifest(manifest)


def _summary_kernel_deep(_capture_dir: Path, manifest: dict[str, Any]) -> str:
    return _kernel_deep_analyze_from_manifest(manifest)


def _summary_instructions(capture_dir: Path, _manifest: dict[str, Any]) -> str:
    dispatch_dir = _find_att_dispatch_dir(capture_dir)
    if dispatch_dir is None:
        return "error: no decoded ATT dispatch directory found under this capture"
    return run_cli(att.cmd_hotspots, dispatch_dir=str(dispatch_dir), top=15)


_SUMMARY_DISPATCH: dict[str, Callable[[Path, dict[str, Any]], str]] = {
    "timeline": _summary_timeline,
    "counters": _summary_counters,
    "kernel_deep": _summary_kernel_deep,
    "instructions": _summary_instructions,
    "ops": _ops_summary,
}


def summary(capture: str) -> str:
    """All-in-one analysis of one capture, dispatched by its recorded kind.

    Args:
        capture: A capture id (from a ``profile_*`` tool or ``captures()``),
            or an explicit capture directory path.
    """
    try:
        capture_dir = capture_runtime.resolve(capture)
        manifest = capture_runtime.load_manifest(capture_dir)
    except (FileNotFoundError, ValueError) as exc:
        return f"error: {exc}"
    handler = _SUMMARY_DISPATCH.get(manifest.get("kind"))
    if handler is None:
        return (
            f"error: unknown or unsupported capture kind {manifest.get('kind')!r} for {capture_dir}"
        )
    return handler(capture_dir, manifest)


# ---------------------------------------------------------------------------
# compare: diff two captures of the same kind
# ---------------------------------------------------------------------------


def _delta_rows(
    totals_a: dict[str, float], totals_b: dict[str, float]
) -> list[tuple[str, float, float, float]]:
    """(name, a_value, b_value, b-a) for every name in either side, ranked by |delta| descending.

    Antisymmetric by construction: swapping the two inputs negates every
    row's delta (and a_value/b_value swap), which lets a property test
    verify ``compare`` without depending on rocprofv3 output at all.
    """
    names = set(totals_a) | set(totals_b)
    return sorted(
        (
            (
                name,
                totals_a.get(name, 0.0),
                totals_b.get(name, 0.0),
                totals_b.get(name, 0.0) - totals_a.get(name, 0.0),
            )
            for name in names
        ),
        key=lambda row: abs(row[3]),
        reverse=True,
    )


def _compare_timeline(dir_a: Path, dir_b: Path) -> str:
    # Both sides compare against their own recorded load phase (falling
    # back to the whole run when a capture has none / can't be aligned --
    # see analyze_rocprof.resolve_window): comparing a's steady-state
    # numbers against b's startup-polluted ones would be meaningless.
    kernels_a = analyze_rocprof.kernel_time_totals(str(dir_a), window="load")
    kernels_b = analyze_rocprof.kernel_time_totals(str(dir_b), window="load")
    families_a = analyze_rocprof.family_time_totals(str(dir_a), window="load")
    families_b = analyze_rocprof.family_time_totals(str(dir_b), window="load")

    lines = ["Family time deltas (b - a, nanoseconds):"]
    for name, va, vb, delta in _delta_rows(families_a, families_b)[:_TOP_DELTA_ROWS]:
        lines.append(f"  {name:<28} a={va:>14,.0f}  b={vb:>14,.0f}  delta={delta:>+14,.0f}")

    lines.append("\nTop kernel time deltas (b - a, nanoseconds):")
    for name, va, vb, delta in _delta_rows(kernels_a, kernels_b)[:_TOP_DELTA_ROWS]:
        lines.append(f"  {name[:60]:<60} a={va:>14,.0f}  b={vb:>14,.0f}  delta={delta:>+14,.0f}")

    new_kernels = sorted(set(kernels_b) - set(kernels_a))
    removed_kernels = sorted(set(kernels_a) - set(kernels_b))
    if new_kernels:
        lines.append(f"\nNew kernels in b (not in a): {len(new_kernels)}")
        lines.extend(f"  + {name[:80]}" for name in new_kernels[:_TOP_DELTA_ROWS])
    if removed_kernels:
        lines.append(f"\nKernels removed in b (present in a): {len(removed_kernels)}")
        lines.extend(f"  - {name[:80]}" for name in removed_kernels[:_TOP_DELTA_ROWS])
    return "\n".join(lines)


def _fmt_optional(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.4f}" if signed else f"{value:.4f}"


def _compare_counters(manifest_a: dict[str, Any], manifest_b: dict[str, Any]) -> str:
    set_dirs_a, set_dirs_b = manifest_a.get("set_dirs"), manifest_b.get("set_dirs")
    arch_a, arch_b = manifest_a.get("arch"), manifest_b.get("arch")
    if not set_dirs_a or not set_dirs_b or not arch_a or not arch_b:
        return "error: counters manifest(s) missing set_dirs/arch"
    if arch_a != arch_b:
        return f"error: cannot compare counters captures from different architectures ({arch_a!r} vs {arch_b!r})"
    metrics_a = counters.kernel_metrics_by_name(list(set_dirs_a.values()), arch=arch_a)
    metrics_b = counters.kernel_metrics_by_name(list(set_dirs_b.values()), arch=arch_b)

    names = sorted(set(metrics_a) | set(metrics_b))
    lines = [f"Counter metric deltas for {arch_a} (b - a):"]
    for name in names:
        metric_a, metric_b = metrics_a.get(name), metrics_b.get(name)
        lines.append(f"\n{name[:80]}")
        for field_name in _COUNTER_METRIC_FIELDS:
            va = getattr(metric_a, field_name, None) if metric_a else None
            vb = getattr(metric_b, field_name, None) if metric_b else None
            delta = (vb - va) if va is not None and vb is not None else None
            lines.append(
                f"  {field_name:<24} a={_fmt_optional(va)}  b={_fmt_optional(vb)}  "
                f"delta={_fmt_optional(delta, signed=True)}"
            )
    return "\n".join(lines)


def compare(a: str, b: str) -> str:
    """Diff two captures of the same kind: top kernel/family deltas, or counter-metric deltas.

    Args:
        a: The baseline capture id or path.
        b: The candidate capture id or path; every delta is ``b - a``
            (negative means ``b`` used less time / a lower rate than ``a``).
    """
    try:
        dir_a, dir_b = capture_runtime.resolve(a), capture_runtime.resolve(b)
        manifest_a = capture_runtime.load_manifest(dir_a)
        manifest_b = capture_runtime.load_manifest(dir_b)
    except (FileNotFoundError, ValueError) as exc:
        return f"error: {exc}"
    kind_a, kind_b = manifest_a.get("kind"), manifest_b.get("kind")
    if kind_a != kind_b:
        return f"error: cannot compare captures of different kinds ({kind_a!r} vs {kind_b!r})"
    if kind_a == "timeline":
        return _compare_timeline(dir_a, dir_b)
    if kind_a == "counters":
        return _compare_counters(manifest_a, manifest_b)
    return (
        f"error: compare is not implemented for {kind_a!r} captures; call summary({a!r}) and "
        f"summary({b!r}) and compare manually."
    )
