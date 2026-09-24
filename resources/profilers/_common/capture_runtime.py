"""Shared, engine-agnostic capture-lifecycle runtime for profiler plugins.

Every ``profile_*`` MCP tool (``profile_timeline``, ``profile_counters``,
``profile_kernel_deep``, ``profile_instructions``, ``profile_ops``, ...)
drives the same generic lifecycle: start a target command under a profiler,
optionally wait for it to become ready and apply a load generator, then stop
it cleanly so the profiler can flush its trace, escalating to a forceful
kill only if the target refuses to exit. This module owns that lifecycle so
no per-profiler tool code has to reimplement process supervision.

Nothing here knows about any specific serving engine: ``command``,
``ready_command``, and ``load_command`` are opaque shell strings supplied by
the calling tool (ultimately chosen by the agent). Engine-specific knowledge
belongs in skill docs and prompt templates, not here.

Standalone module: stdlib only, Python 3.10+, no ``vibesys`` imports. It is
staged alongside every profiler plugin (see
``docs/contributing/amd-profiler-worklog.md`` and the profiler packaging
tests) so it must run both from a repository checkout
(``resources/profilers/_common/capture_runtime.py``, a sibling of each
``resources/profilers/<kind>/``) and from an agent workspace, where it is
staged as ``<workspace>/profilers_common/capture_runtime.py``, a sibling of
each ``<workspace>/<kind>_profiler/``. A plugin's own ``server.py`` finds it
with a small path shim, e.g.::

    import sys
    from pathlib import Path

    _HERE = Path(__file__).resolve().parent
    for _name in ("_common", "profilers_common"):
        _candidate = _HERE.parent / _name
        if (_candidate / "capture_runtime.py").is_file():
            sys.path.insert(0, str(_candidate))
            break
    import capture_runtime  # noqa: E402
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import secrets
import signal
import socket
import stat
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "CaptureResult",
    "CaptureStatus",
    "CaptureSummary",
    "Lifecycle",
    "format_result",
    "list_captures",
    "load_manifest",
    "new_capture",
    "profiles_root",
    "resolve",
    "run_capture",
    "write_manifest",
]

_MANIFEST_NAME = "manifest.json"
_TARGET_LOG_NAME = "target.log"
_LOAD_LOG_NAME = "load.log"
_ESCALATION_WAIT_S = 2.0

# Lifecycle commands are written to script files rather than passed as
# inline ``bash -lc <text>`` argv (see ``_write_script``'s docstring for
# why); one script name per lifecycle command.
_TARGET_SCRIPT_NAME = "target.sh"
_READY_SCRIPT_NAME = "ready.sh"
_LOAD_SCRIPT_NAME = "load.sh"
_SCRIPT_SHEBANG = "#!/usr/bin/env bash\n"
_SCRIPT_EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


class CaptureStatus(str, Enum):
    """Terminal outcome of one ``run_capture`` call."""

    OK = "ok"
    TARGET_FAILED = "target_failed"
    NOT_READY = "not_ready"
    LOAD_FAILED = "load_failed"
    TIMED_OUT = "timed_out"
    KILLED_AFTER_GRACE = "killed_after_grace"


@dataclass(frozen=True)
class Lifecycle:
    """Generic start/ready/load/stop lifecycle for one capture target.

    ``command`` is written to a script file and run via plain ``bash
    <script>`` (see ``run_capture``); it may be arbitrary multi-line shell
    text (heredocs, embedded quotes, `$vars`, ...) since nothing re-tokenizes
    it after this. When ``load_command`` is ``None``,
    the target is expected to exit on its own (a bounded benchmark script);
    ``run_capture`` just waits for it, up to ``timeout_s``. When
    ``load_command`` is set, the target is expected to keep running (a
    server) until ``run_capture`` polls ``ready_command`` to completion,
    runs ``load_command`` against it, then stops it with ``stop_signal``.
    """

    command: str
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    ready_command: str | None = None
    ready_timeout_s: float = 60.0
    ready_interval_s: float = 1.0
    load_command: str | None = None
    stop_signal: str = "SIGINT"
    grace_s: float = 10.0
    timeout_s: float = 300.0


@dataclass(frozen=True)
class CaptureResult:
    """Outcome of one ``run_capture`` call, sized for a prompt reply."""

    capture_id: str
    kind: str
    status: CaptureStatus
    out_dir: Path
    target_returncode: int | None
    load_returncode: int | None
    ready_achieved: bool | None
    escalated: bool
    timings: dict[str, float]
    target_log_tail: str
    load_log_tail: str | None
    manifest_path: Path


@dataclass(frozen=True)
class CaptureSummary:
    """One row of ``list_captures``."""

    capture_id: str
    dir: Path
    kind: str | None
    status: str | None


@dataclass
class _Outcome:
    status: CaptureStatus
    target_returncode: int | None
    load_returncode: int | None = None
    escalated: bool = False
    ready_achieved: bool | None = None
    load_tail: str | None = None
    # Wall-clock/monotonic timestamps bracketing the load phase (the window
    # between ready_command succeeding and load_command finishing), or None
    # when there is no load_command (a bounded script has no separate
    # "startup" vs. "steady state" phase to distinguish). See
    # ``_clock_stamps`` for the fields each phase's stamp carries.
    load_window: dict[str, dict[str, int]] | None = None


@dataclass(frozen=True)
class _ManifestContext:
    capture_id: str
    kind: str
    argv: list[str]
    lifecycle: Lifecycle
    out_dir: Path
    meta: dict[str, Any]
    outcome: _Outcome
    timings: dict[str, float]
    scripts: dict[str, str]
    capture_start: dict[str, int]
    capture_end: dict[str, int]


# -- capture store ------------------------------------------------------------


def _profiles_root() -> Path:
    """The capture store root, always resolved to an absolute path.

    ``new_capture``'s returned directory is passed both to the profiled
    subprocess (as the profiler's ``-d`` output-directory argument, a
    process started with ``cwd=lifecycle.cwd``) and used directly by this
    process's own analysis code afterward. A relative default
    (``./.profiles``) is ambiguous between those two: the subprocess
    resolves it against ``lifecycle.cwd`` while this process resolves it
    against its own cwd, and those differ whenever a capture tool's
    ``cwd`` argument names anything other than the server's own launch
    directory -- the profiler then writes its whole output tree somewhere
    this process never looks, and every downstream ``summary``/analyzer
    call sees an empty capture. Resolving here, once, before the path is
    handed to either side, keeps both sides pointed at the same directory
    regardless of ``lifecycle.cwd``.
    """
    raw = os.environ.get("VIBESYS_PROFILE_DIR", "./.profiles")
    return Path(raw).expanduser().resolve()


def profiles_root() -> Path:
    """Public accessor for the capture store root (``$VIBESYS_PROFILE_DIR``, default ``./.profiles``)."""
    return _profiles_root()


def new_capture(kind: str) -> tuple[str, Path]:
    """Allocate a fresh capture id + directory under ``$VIBESYS_PROFILE_DIR``."""
    root = _profiles_root()
    root.mkdir(parents=True, exist_ok=True)
    for _attempt in range(10):
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        capture_id = f"{kind}-{timestamp}-{secrets.token_hex(2)}"
        directory = root / capture_id
        try:
            directory.mkdir(parents=False)
        except FileExistsError:
            continue
        return capture_id, directory
    raise RuntimeError("could not allocate a unique capture directory")  # noqa: TRY003


def write_manifest(directory: Path, manifest: dict[str, Any]) -> Path:
    """Write ``manifest.json`` into *directory* and return its path."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n")
    return path


def resolve(capture_id_or_path: str) -> Path:
    """Resolve a capture id or explicit path to its capture directory."""
    candidate = Path(capture_id_or_path).expanduser()
    if candidate.is_dir():
        return candidate.resolve()
    root = _profiles_root()
    by_id = root / capture_id_or_path
    if by_id.is_dir():
        return by_id.resolve()
    raise FileNotFoundError(  # noqa: TRY003
        f"capture not found: {capture_id_or_path!r} (checked that path and {root})"
    )


def load_manifest(capture_dir: Path) -> dict[str, Any]:
    """Load ``manifest.json`` from *capture_dir*, raising a clear error if absent/malformed."""
    path = Path(capture_dir) / _MANIFEST_NAME
    if not path.is_file():
        raise FileNotFoundError(f"no manifest.json under {capture_dir}")  # noqa: TRY003
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed manifest.json under {capture_dir}: {exc}") from exc  # noqa: TRY003
    if isinstance(data, dict):
        return data
    raise ValueError(f"manifest.json under {capture_dir} is not a JSON object")  # noqa: TRY003


def list_captures(limit: int = 20) -> list[CaptureSummary]:
    """Return the most recently modified captures, newest first."""
    root = _profiles_root()
    if not root.is_dir():
        return []
    entries = [p for p in root.iterdir() if p.is_dir()]
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    summaries: list[CaptureSummary] = []
    for entry in entries[:limit]:
        kind: str | None = None
        status: str | None = None
        manifest_path = entry / _MANIFEST_NAME
        if manifest_path.is_file():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                data = json.loads(manifest_path.read_text())
                kind = data.get("kind")
                status = data.get("status")
        summaries.append(CaptureSummary(capture_id=entry.name, dir=entry, kind=kind, status=status))
    return summaries


def format_result(result: CaptureResult, *, max_log_chars: int = 800) -> str:
    """A prompt-sized summary of *result*: status, timings, capped log tail."""
    lines = [
        f"capture {result.capture_id} ({result.kind}): {result.status.value}",
        f"  duration={result.timings.get('duration_s', 0.0):.2f}s "
        f"target_rc={result.target_returncode} load_rc={result.load_returncode} "
        f"escalated={result.escalated}",
        f"  manifest={result.manifest_path}",
    ]
    tail = result.target_log_tail[-max_log_chars:]
    if tail:
        lines.append("  target log tail:")
        lines.extend(f"    {line}" for line in tail.splitlines()[-20:])
    if result.load_log_tail:
        load_tail = result.load_log_tail[-max_log_chars:]
        lines.append("  load log tail:")
        lines.extend(f"    {line}" for line in load_tail.splitlines()[-20:])
    return "\n".join(lines)


# -- process lifecycle ---------------------------------------------------------


def _effective_env(lifecycle: Lifecycle) -> dict[str, str]:
    return {**os.environ, **lifecycle.env}


def _remaining(start: float, timeout_s: float) -> float:
    return max(0.0, timeout_s - (time.monotonic() - start))


def _tail(path: Path, *, max_chars: int = 4000) -> str:
    if not path.is_file():
        return ""
    text = path.read_bytes().decode("utf-8", errors="replace")
    return text if len(text) <= max_chars else text[-max_chars:]


def _write_script(directory: Path, name: str, text: str) -> Path:
    """Write *text* verbatim (after a shebang line) as an executable script.

    A lifecycle command used to be passed as inline argv text (``bash -lc
    "<text>"``) to whatever ``profiler_prefix`` wraps it. That is fragile in
    general: a multi-line command with embedded quotes depends on every
    layer between here and the final ``bash`` treating the trailing argv as
    fully opaque, which is not guaranteed of every profiler wrapper. Writing
    the command to a script file and handing the wrapper a single, simple
    path argument removes that whole class of risk: nothing between here and
    the ``bash`` that finally reads and runs the file re-tokenizes *text*, so
    newlines, quotes, ``$vars``, and heredocs all survive intact regardless
    of what a profiler wrapper does to simple argv tokens. (A real rocprofv3
    incident this branch also hardens against turned out to have a different
    root cause -- rocprofv3 injecting itself into every descendant process,
    including a `$(...)`-captured helper, and one of those descendants
    printing a diagnostic banner into the value being captured; see
    ``resources/profilers/rocprof/capture.py``'s ``_ROCPROFV3_QUIET_ENV`` and
    the serving-engines profiling doc for that fix.)

    *text* is preserved byte-for-byte after the shebang line: no ``set -e``
    or other semantics are injected.
    """
    path = directory / name
    path.write_text(_SCRIPT_SHEBANG + text)
    path.chmod(path.stat().st_mode | _SCRIPT_EXEC_BITS)
    return path


def _clock_stamps() -> dict[str, int]:
    """Wall-clock + monotonic readings of "now", for later clock alignment.

    Records ``time.monotonic_ns()``, the raw ``time.clock_gettime_ns(
    time.CLOCK_MONOTONIC)`` reading (implemented the same way on
    CPython/Linux, but both kept since a downstream analyzer may only trust
    one), and ``time.clock_gettime_ns(time.CLOCK_REALTIME)`` (wall-clock
    epoch ns). An external tool's own trace timestamps (e.g. rocprofv3's
    system-trace CSV ``Start_Timestamp``/``End_Timestamp`` columns, verified
    against real MI210 captures to be CLOCK_MONOTONIC-based ns -- see
    ``docs/contributing/amd-profiler-worklog.md``) can then be compared
    directly against ``clock_monotonic_ns`` when both processes ran on the
    same host, without any offset math. A downstream analyzer that cannot
    verify this alignment for a given capture must fall back to analyzing
    the whole run rather than silently mis-slicing it.
    """
    return {
        "monotonic_ns": time.monotonic_ns(),
        "clock_monotonic_ns": time.clock_gettime_ns(time.CLOCK_MONOTONIC),
        "realtime_ns": time.clock_gettime_ns(time.CLOCK_REALTIME),
    }


def _start_process(
    lifecycle: Lifecycle, profiler_prefix: list[str], log_path: Path, script_path: Path
) -> subprocess.Popen[bytes]:
    argv = [*profiler_prefix, "bash", str(script_path)]
    with log_path.open("wb") as handle:
        return subprocess.Popen(  # noqa: S603  # tracked: #288
            argv,
            cwd=lifecycle.cwd,
            env=_effective_env(lifecycle),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _run_no_load(proc: subprocess.Popen[bytes], lifecycle: Lifecycle, start: float) -> _Outcome:
    try:
        rc = proc.wait(timeout=_remaining(start, lifecycle.timeout_s))
    except subprocess.TimeoutExpired:
        _escalate(proc)
        return _Outcome(CaptureStatus.TIMED_OUT, None, escalated=True)
    status = CaptureStatus.OK if rc == 0 else CaptureStatus.TARGET_FAILED
    return _Outcome(status, rc)


def _run_check(script_path: Path, lifecycle: Lifecycle, timeout: float) -> int | None:
    try:
        result = subprocess.run(  # noqa: S603  # tracked: #288
            ["bash", str(script_path)],  # noqa: S607  # tracked: #288
            cwd=lifecycle.cwd,
            env=_effective_env(lifecycle),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=max(timeout, 0.01),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    return result.returncode


def _poll_ready(
    proc: subprocess.Popen[bytes], lifecycle: Lifecycle, start: float, ready_script: Path
) -> tuple[bool, bool]:
    """Poll ``ready_command`` (as *ready_script*) until it succeeds.

    Return (ready, target_exited_early).
    """
    assert lifecycle.ready_command is not None  # noqa: S101  # tracked: #288
    ready_start = time.monotonic()
    while True:
        if proc.poll() is not None:
            return False, True
        elapsed_ready = time.monotonic() - ready_start
        remaining_overall = _remaining(start, lifecycle.timeout_s)
        if elapsed_ready >= lifecycle.ready_timeout_s or remaining_overall <= 0:
            return False, False
        check_budget = min(10.0, lifecycle.ready_timeout_s - elapsed_ready, remaining_overall)
        if _run_check(ready_script, lifecycle, check_budget) == 0:
            return True, False
        sleep_for = min(
            lifecycle.ready_interval_s,
            lifecycle.ready_timeout_s - (time.monotonic() - ready_start),
            _remaining(start, lifecycle.timeout_s),
        )
        if sleep_for > 0:
            time.sleep(sleep_for)


def _run_load(
    lifecycle: Lifecycle, timeout: float, out_dir: Path, load_script: Path
) -> tuple[int | None, str, bool]:
    assert lifecycle.load_command is not None  # noqa: S101  # tracked: #288
    log_path = out_dir / _LOAD_LOG_NAME
    with log_path.open("wb") as handle:
        proc = subprocess.Popen(  # noqa: S603  # tracked: #288
            ["bash", str(load_script)],  # noqa: S607  # tracked: #288
            cwd=lifecycle.cwd,
            env=_effective_env(lifecycle),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        rc = proc.wait(timeout=max(timeout, 0.0))
    except subprocess.TimeoutExpired:
        _escalate(proc)
        return None, _tail(log_path), True
    return rc, _tail(log_path), False


def _stop_and_wait_grace(proc: subprocess.Popen[bytes], lifecycle: Lifecycle) -> tuple[bool, bool]:
    """Send ``stop_signal`` and wait ``grace_s``. Return (exited, escalated)."""
    if proc.poll() is not None:
        return True, False
    _send_stop_signal(proc.pid, lifecycle.stop_signal)
    try:
        proc.wait(timeout=max(lifecycle.grace_s, 0.0))
    except subprocess.TimeoutExpired:
        _escalate(proc)
        return False, True
    return True, False


def _run_with_load(  # noqa: PLR0913  # tracked: #288
    proc: subprocess.Popen[bytes],
    lifecycle: Lifecycle,
    start: float,
    out_dir: Path,
    ready_script: Path,
    load_script: Path,
) -> _Outcome:
    ready, target_exited_early = _poll_ready(proc, lifecycle, start, ready_script)
    if target_exited_early:
        return _Outcome(CaptureStatus.TARGET_FAILED, proc.returncode, ready_achieved=False)

    if not ready:
        _exited, escalated = _stop_and_wait_grace(proc, lifecycle)
        status = (
            CaptureStatus.TIMED_OUT
            if _remaining(start, lifecycle.timeout_s) <= 0
            else CaptureStatus.NOT_READY
        )
        return _Outcome(status, proc.returncode, escalated=escalated, ready_achieved=False)

    # The load phase (ready_command succeeded -> load_command finishes) is
    # the "steady state" window a capture's later analysis wants, distinct
    # from everything before it (server startup: weight load, warmup, KV
    # init, ...). Bracket it with clock stamps so an analyzer can slice a
    # timeline capture down to just this window instead of the whole run.
    ready_stamp = _clock_stamps()
    load_start_stamp = _clock_stamps()
    remaining = _remaining(start, lifecycle.timeout_s)
    load_rc, load_tail, load_timed_out = _run_load(lifecycle, remaining, out_dir, load_script)
    load_window = {"ready": ready_stamp, "start": load_start_stamp, "end": _clock_stamps()}
    if load_timed_out or load_rc != 0:
        _exited, escalated = _stop_and_wait_grace(proc, lifecycle)
        status = CaptureStatus.TIMED_OUT if load_timed_out else CaptureStatus.LOAD_FAILED
        return _Outcome(
            status,
            proc.returncode,
            load_returncode=load_rc,
            escalated=escalated,
            ready_achieved=True,
            load_tail=load_tail,
            load_window=load_window,
        )

    exited, escalated = _stop_and_wait_grace(proc, lifecycle)
    status = CaptureStatus.OK if exited else CaptureStatus.KILLED_AFTER_GRACE
    return _Outcome(
        status,
        proc.returncode,
        load_returncode=load_rc,
        escalated=escalated,
        ready_achieved=True,
        load_tail=load_tail,
        load_window=load_window,
    )


# -- forceful cleanup: never leave a stray process ------------------------------


def _resolve_signal(name: str) -> signal.Signals:
    try:
        return signal.Signals[name]
    except KeyError as exc:
        raise ValueError(f"unknown stop_signal: {name!r}") from exc  # noqa: TRY003


def _send_stop_signal(pid: int, name: str) -> None:
    sig = _resolve_signal(name)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(pid), sig)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ppid_of(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    closing = stat.rfind(")")
    if closing == -1:
        return None
    fields = stat[closing + 2 :].split()
    if len(fields) < 2:  # noqa: PLR2004
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def _descendants(root_pid: int) -> set[int]:
    """All descendants of *root_pid*, walking /proc so setsid children are found too."""
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return set()
    children: dict[int, list[int]] = {}
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        ppid = _ppid_of(pid)
        if ppid is not None:
            children.setdefault(ppid, []).append(pid)
    result: set[int] = set()
    frontier = [root_pid]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, ()):
            if child not in result:
                result.add(child)
                frontier.append(child)
    return result


def _kill_tree(pid: int, descendants: set[int], sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(pid), sig)
    for target_pid in {pid, *descendants}:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(target_pid, sig)


def _reap(proc: subprocess.Popen[bytes], timeout: float) -> bool:
    """Wait for *proc* (our own child) to exit, reaping it. False on timeout.

    Reaping our own child through ``Popen.wait`` matters, not just signaling
    it: until we do, it is a zombie, and ``os.kill(pid, 0)`` (what liveness
    checks below use for processes we do *not* own) keeps reporting it alive
    forever, since a zombie still holds its PID table slot.
    """
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _wait_for_others_death(pids: set[int], timeout: float) -> bool:
    """Poll liveness of descendants we do not directly own (not our child).

    Once their original parent has actually terminated, the kernel
    reparents them to an ancestor that reaps orphans (init or a subreaper),
    so a short poll is enough once *our* child (the tree root) is reaped.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_alive(pid) for pid in pids):
            return True
        time.sleep(0.05)
    return not any(_alive(pid) for pid in pids)


def _escalate(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM, then SIGKILL, the whole process tree rooted at *proc*."""
    pid = proc.pid
    descendants = _descendants(pid)
    _kill_tree(pid, descendants, signal.SIGTERM)
    if _reap(proc, _ESCALATION_WAIT_S) and _wait_for_others_death(descendants, 0.5):
        return
    descendants |= _descendants(pid)
    _kill_tree(pid, descendants, signal.SIGKILL)
    _reap(proc, _ESCALATION_WAIT_S)
    _wait_for_others_death(descendants, _ESCALATION_WAIT_S)


# -- manifest -------------------------------------------------------------------


def _redact_lifecycle(lifecycle: Lifecycle) -> dict[str, Any]:
    payload = dataclasses.asdict(lifecycle)
    payload["env_keys"] = sorted(lifecycle.env)
    del payload["env"]
    return payload


def _git_head(cwd: str | None) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603  # tracked: #288
            ["git", "-C", cwd or ".", "rev-parse", "HEAD"],  # noqa: S607  # tracked: #288
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _output_files(out_dir: Path) -> list[str]:
    return sorted(
        str(p.relative_to(out_dir))
        for p in out_dir.rglob("*")
        if p.is_file() and p.name != _MANIFEST_NAME
    )


def _build_manifest(ctx: _ManifestContext) -> dict[str, Any]:
    outcome = ctx.outcome
    return {
        "capture_id": ctx.capture_id,
        "kind": ctx.kind,
        "profiler_command": ctx.argv,
        "lifecycle": _redact_lifecycle(ctx.lifecycle),
        "cwd": ctx.lifecycle.cwd,
        "hostname": socket.gethostname(),
        "timings": ctx.timings,
        "status": outcome.status.value,
        "target_returncode": outcome.target_returncode,
        "load_returncode": outcome.load_returncode,
        "ready_achieved": outcome.ready_achieved,
        "escalated": outcome.escalated,
        "meta": ctx.meta,
        "git_head": _git_head(ctx.lifecycle.cwd),
        "output_files": _output_files(ctx.out_dir),
        "scripts": ctx.scripts,
        "load_window": outcome.load_window,
        # Bracket the whole capture (not just the load phase) so an
        # analyzer can also reconstruct a "startup" window
        # (capture_start .. load_window["start"], i.e. everything before
        # load_command ran: server boot, weight load, warmup, KV init) and
        # sanity-check clock alignment (do the profiled tool's own trace
        # timestamps fall inside this same span, same clock domain) before
        # trusting a "load"/"startup" slice at all.
        "capture_start": ctx.capture_start,
        "capture_end": ctx.capture_end,
    }


# -- entry point ------------------------------------------------------------


def run_capture(
    profiler_prefix: list[str],
    lifecycle: Lifecycle,
    *,
    kind: str,
    out_dir: Path,
    meta: dict[str, Any],
) -> CaptureResult:
    """Run one profiler-wrapped capture through its full lifecycle.

    Always leaves the process tree clean: the target (and, when
    ``load_command`` is set, the load generator) is either waited for to a
    natural exit or escalated through SIGTERM/SIGKILL across its whole
    descendant tree, never left running past this call. Writes
    ``manifest.json`` into *out_dir* before returning.

    ``command`` (and ``ready_command``/``load_command``, when given) are
    written to script files under *out_dir* rather than passed as inline
    ``bash -lc <text>`` argv -- see ``_write_script``'s docstring -- and run
    as ``profiler_prefix + ["bash", <script path>]``. Their paths (relative
    to *out_dir*) are recorded in the manifest under ``"scripts"``.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    capture_id = out_dir.name

    target_script = _write_script(out_dir, _TARGET_SCRIPT_NAME, lifecycle.command)
    scripts = {"target": target_script.name}
    ready_script: Path | None = None
    if lifecycle.ready_command is not None:
        ready_script = _write_script(out_dir, _READY_SCRIPT_NAME, lifecycle.ready_command)
        scripts["ready"] = ready_script.name
    load_script: Path | None = None
    if lifecycle.load_command is not None:
        load_script = _write_script(out_dir, _LOAD_SCRIPT_NAME, lifecycle.load_command)
        scripts["load"] = load_script.name

    argv = [*profiler_prefix, "bash", str(target_script)]
    target_log_path = out_dir / _TARGET_LOG_NAME

    start = time.monotonic()
    capture_start = _clock_stamps()
    timings: dict[str, float] = {"started_at": time.time()}
    proc = _start_process(lifecycle, profiler_prefix, target_log_path, target_script)

    if lifecycle.load_command is None:
        outcome = _run_no_load(proc, lifecycle, start)
    else:
        assert ready_script is not None  # noqa: S101  # tracked: #288
        assert load_script is not None  # noqa: S101  # tracked: #288
        outcome = _run_with_load(proc, lifecycle, start, out_dir, ready_script, load_script)

    timings["finished_at"] = time.time()
    timings["duration_s"] = time.monotonic() - start
    capture_end = _clock_stamps()

    manifest = _build_manifest(
        _ManifestContext(
            capture_id=capture_id,
            kind=kind,
            argv=argv,
            lifecycle=lifecycle,
            out_dir=out_dir,
            meta=meta,
            outcome=outcome,
            timings=timings,
            scripts=scripts,
            capture_start=capture_start,
            capture_end=capture_end,
        )
    )
    manifest_path = write_manifest(out_dir, manifest)

    return CaptureResult(
        capture_id=capture_id,
        kind=kind,
        status=outcome.status,
        out_dir=out_dir,
        target_returncode=outcome.target_returncode,
        load_returncode=outcome.load_returncode,
        ready_achieved=outcome.ready_achieved,
        escalated=outcome.escalated,
        timings=timings,
        target_log_tail=_tail(target_log_path),
        load_log_tail=outcome.load_tail,
        manifest_path=manifest_path,
    )
