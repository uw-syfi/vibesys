"""A scripted ``ProcessHost`` for the profiler capture lifecycle: processes on a virtual clock.

``capture_runtime.run_capture`` and the warm-target registry start shell scripts, poll
readiness checks, send stop signals and wait against deadlines. This module is the test
side of that role (``capture_runtime.ProcessHost``): a :class:`ScriptedHost` installed
with ``capture_runtime.use_host`` answers every spawn, check, signal and clock read from
Python, so a capture that "takes" minutes runs instantly and identically every time.

A test describes what each lifecycle script *does* with a :data:`Script` (a callable that
receives the started :class:`FakeProcess` and schedules its behaviour), keyed by the
script's text (``Lifecycle.command`` and friends, exactly as the lifecycle writes them).
Builders cover the common behaviours (:func:`exits`, :func:`serves`); anything else is a
few lines of ``proc.after`` / ``proc.on_signal``.

Only the lifecycle's contract is faked here. That real shell scripts, real signals and a
real process tree behave the same way is checked once, against real processes, in
``tests/e2e/test_profiler_capture_processes_e2e.py``.
"""

from __future__ import annotations

import functools
import gzip
import heapq
import itertools
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import resources.profilers._common.capture_runtime as cr

_SIGNAL_NUMBERS = {
    "SIGHUP": 1,
    "SIGINT": 2,
    "SIGKILL": 9,
    "SIGUSR1": 10,
    "SIGUSR2": 12,
    "SIGTERM": 15,
}
_SHEBANG = "#!/usr/bin/env bash\n"
_TARGET_TRAP = "trap '' SIGUSR1 SIGUSR2\n"
_WAITS_FOREVER = "a wait with no deadline on a process nothing will ever stop would block forever"
_FIRST_PID = 1000
_WALL_EPOCH = 1_700_000_000.0
_ESCALATION_WAIT_S = 2.0

# What a process does with a signal it handles: run this when it arrives.
type Reaction = Callable[[], None]
# What a lifecycle script does: schedule behaviour on the freshly started process.
type Script = Callable[[FakeProcess], None]
# A readiness or other check: its exit status at the moment it runs (None: it hangs).
type Check = Callable[[], int | None]
# A host-capability query (``rocprofv3 --version``): its status and output for this argv.
type Probe = Callable[[list[str]], tuple[int | None, str]]


class VirtualClock:
    """Seconds that pass only when something waits, running scheduled events as they fall due."""

    def __init__(self) -> None:
        self.now = 0.0
        self._events: list[tuple[float, int, Callable[[], None]]] = []
        self._order = itertools.count()

    def at(self, when: float, event: Callable[[], None]) -> None:
        """Run *event* when the clock reaches *when*."""
        heapq.heappush(self._events, (when, next(self._order), event))

    def after(self, delay: float, event: Callable[[], None]) -> None:
        """Run *event* *delay* seconds from now."""
        self.at(self.now + delay, event)

    def advance_to(self, target: float) -> None:
        """Move to *target*, running every event that falls due on the way, in order."""
        while self._events and self._events[0][0] <= target:
            when, _, event = heapq.heappop(self._events)
            self.now = max(self.now, when)
            event()
        self.now = max(self.now, target)

    def wait_until(self, done: Callable[[], bool], timeout: float | None) -> bool:
        """Let time pass until ``done()`` holds or *timeout* seconds elapsed; whether it holds."""
        deadline = None if timeout is None else self.now + max(timeout, 0.0)
        while not done():
            if self._events and (deadline is None or self._events[0][0] <= deadline):
                self.advance_to(self._events[0][0])
            elif deadline is None:
                raise AssertionError(_WAITS_FOREVER)
            else:
                self.advance_to(deadline)
                return done()
        return True


@dataclass
class Launch:
    """What a process was started with."""

    argv: list[str]
    cwd: str | None
    env: dict[str, str]
    log_path: Path | None = None

    @property
    def profiler_prefix(self) -> list[str]:
        """The wrapper argv in front of ``bash <script>`` (empty for an unprofiled step)."""
        return self.argv[:-2]


@dataclass
class Spawn:
    """One process the lifecycle started: its launch, the script text it ran, and the process."""

    launch: Launch
    script_text: str
    process: FakeProcess

    @property
    def argv(self) -> list[str]:
        """The full argv."""
        return self.launch.argv

    @property
    def env(self) -> dict[str, str]:
        """The environment it was started with."""
        return self.launch.env

    @property
    def profiler_prefix(self) -> list[str]:
        """The wrapper argv in front of ``bash <script>`` (empty for an unprofiled step)."""
        return self.launch.profiler_prefix


class FakeProcess:
    """A started process whose behaviour is whatever its :data:`Script` scheduled."""

    def __init__(self, host: ScriptedHost, pid: int, launch: Launch) -> None:
        self.host = host
        self.pid = pid
        self.launch = launch
        self.argv = launch.argv
        self.env = launch.env
        self.log_path = launch.log_path
        self.returncode: int | None = None
        self.signals: list[str] = []
        self.children: list[FakeProcess] = []
        self._reactions: dict[str, Reaction | None] = {}
        self._exit_hooks: list[Callable[[], None]] = []

    @property
    def alive(self) -> bool:
        """Whether the process has not exited."""
        return self.returncode is None

    def poll(self) -> int | None:
        """Its exit status, or ``None`` while it runs."""
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """Let time pass until it exits; ``subprocess.TimeoutExpired`` when *timeout* ran out."""
        if not self.host.clock.wait_until(lambda: self.returncode is not None, timeout):
            raise subprocess.TimeoutExpired(self.argv, timeout if timeout is not None else 0.0)
        assert self.returncode is not None
        return self.returncode

    def log(self, text: str) -> None:
        """Append *text* to the process's captured output."""
        if self.log_path is not None and text:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(text)

    def exit(self, code: int) -> None:
        """Exit now with *code* (a no-op once exited)."""
        if self.returncode is not None:
            return
        self.returncode = code
        for hook in self._exit_hooks:
            hook()

    def after(self, delay: float, action: Callable[[], None]) -> None:
        """Run *action* *delay* seconds from now, unless the process exited by then."""
        self.host.clock.after(delay, lambda: action() if self.alive else None)

    def on_exit(self, hook: Callable[[], None]) -> None:
        """Run *hook* when the process exits."""
        self._exit_hooks.append(hook)

    def on_signal(self, name: str, reaction: Reaction | None) -> None:
        """Handle *name* with *reaction*; ``None`` ignores it. ``SIGKILL`` cannot be handled."""
        self._reactions[name] = reaction

    def spawn_child(self, script: Script | None = None) -> FakeProcess:
        """A descendant this process's group owns; it outlives the wrapper unless it exits."""
        child = self.host.new_process(Launch(self.argv, self.launch.cwd, self.env))
        self.children.append(child)
        if script is not None:
            script(child)
        return child

    def deliver(self, name: str) -> None:
        """Deliver signal *name*: its reaction if handled, else the default (terminate)."""
        if not self.alive:
            return
        self.signals.append(name)
        if name != "SIGKILL" and name in self._reactions:
            reaction = self._reactions[name]
            if reaction is not None:
                reaction()
            return
        self.exit(-_SIGNAL_NUMBERS[name])


class FakeGroup:
    """The owned tree rooted at one :class:`FakeProcess`, with the real group's contract."""

    def __init__(self, proc: FakeProcess) -> None:
        self._proc = proc

    def _owned(self) -> list[FakeProcess]:
        return [p for p in [self._proc, *self._proc.children] if p.alive]

    def members(self) -> set[int]:
        """The live processes the capture owns."""
        return {p.pid for p in self._owned()}

    def poll(self) -> int | None:
        """The wrapper's status once it and every owned descendant stopped."""
        if self._proc.returncode is not None and not self._owned():
            return self._proc.returncode
        return None

    def wait(self, timeout_s: float) -> int | None:
        """Wait up to *timeout_s* for the whole tree to stop."""
        self._proc.host.clock.wait_until(lambda: not self._owned(), timeout_s)
        return self.poll()

    def stop(self, signal_name: str) -> None:
        """Deliver *signal_name* to every live owned process."""
        for p in self._owned():
            p.deliver(signal_name)

    def history_complete(self) -> bool:
        """A polling observer cannot prove it saw every process."""
        return False

    def quiesce(self) -> set[int] | None:
        """Nothing here forks; the inventory is the live set."""
        return self.members()

    def resume(self) -> None:
        """Nothing was frozen."""

    def cleanup(self) -> None:
        """Terminate, wait a bounded time, then kill whatever is left."""
        clock = self._proc.host.clock
        for sig in ("SIGTERM", "SIGKILL"):
            self.stop(sig)
            clock.wait_until(lambda: not self._owned(), _ESCALATION_WAIT_S)
            if not self._owned():
                return


class ScriptedHost:
    """A ``capture_runtime.ProcessHost`` that runs lifecycle scripts as :data:`Script` callables.

    ``scripts`` maps a script's text to what it does; ``checks`` maps a check script's text
    (a ``ready_command``) to its exit status over time. A script text with no entry is a
    test error and fails loudly with the text, not a silent success. ``file_checks=True``
    additionally answers the warm-target readiness check (``test -f`` of marker files) from the
    files themselves, since its text embeds a per-capture path no test can know in advance.
    """

    def __init__(
        self,
        scripts: Mapping[str, Script] | None = None,
        checks: Mapping[str, Check] | None = None,
        *,
        check_cost_s: float = 0.01,
        file_checks: bool = False,
        probes: Mapping[str, Probe] | None = None,
    ) -> None:
        self.probes = dict(probes or {})
        self.file_checks = file_checks
        self.clock = VirtualClock()
        self.scripts = dict(scripts or {})
        self.checks = dict(checks or {})
        self.check_cost_s = check_cost_s
        self.spawned: list[Spawn] = []
        self.checks_run: list[tuple[str, int | None]] = []
        self.processes: dict[int, FakeProcess] = {}
        self._pids = itertools.count(_FIRST_PID)

    def new_process(self, launch: Launch) -> FakeProcess:
        """Register a process (the host's own bookkeeping; ``spawn`` is the lifecycle's entry)."""
        proc = FakeProcess(self, next(self._pids), launch)
        self.processes[proc.pid] = proc
        return proc

    @staticmethod
    def _body(script_path: Path) -> tuple[str, bool]:
        text = script_path.read_text()
        assert text.startswith(_SHEBANG), text
        body = text.removeprefix(_SHEBANG)
        trapped = body.startswith(_TARGET_TRAP)
        return body.removeprefix(_TARGET_TRAP), trapped

    def spawn(
        self, argv: list[str], *, cwd: str | None, env: dict[str, str], log_path: Path
    ) -> FakeProcess:
        """Start the script named by the last argv entry as a scripted process."""
        body, trapped = self._body(Path(argv[-1]))
        log_path.write_text("")
        launch = Launch(list(argv), cwd, dict(env), log_path)
        proc = self.new_process(launch)
        if trapped:
            proc.on_signal("SIGUSR1", None)
            proc.on_signal("SIGUSR2", None)
        script = self.scripts.get(body)
        assert script is not None, f"no script registered for lifecycle text: {body!r}"
        self.spawned.append(Spawn(launch, body, proc))
        script(proc)
        return proc

    def check(
        self, argv: list[str], *, cwd: str | None, env: dict[str, str], timeout_s: float
    ) -> int | None:
        """Run a check script: it takes ``check_cost_s`` and reports its scripted status."""
        del cwd, env, timeout_s
        body, _ = self._body(Path(argv[-1]))
        check = self.checks.get(body)
        if check is None and self.file_checks:
            check = functools.partial(_marker_file_status, body)
        assert check is not None, f"no check registered for lifecycle text: {body!r}"
        self.clock.advance_to(self.clock.now + self.check_cost_s)
        status = check()
        self.checks_run.append((body, status))
        return status

    def probe(self, argv: list[str], *, timeout_s: float) -> tuple[int | None, str]:
        """Answer a capability query from ``probes`` by the program's name.

        ``python -c`` queries are keyed ``"python -c"``; a program with no entry behaves as one
        that is not installed.
        """
        del timeout_s
        name = "python -c" if argv[1:2] == ["-c"] else Path(argv[0]).name
        probe = self.probes.get(name)
        if probe is None:
            return None, "executable not found"
        return probe(argv)

    def process_group(
        self, proc: cr.ManagedProcess, identities: dict[int, cr.ProcessIdentity] | None = None
    ) -> cr.CaptureProcessGroup:
        """The owned tree rooted at *proc*."""
        del identities
        assert isinstance(proc, FakeProcess)
        return FakeGroup(proc)

    def pid_alive(self, pid: int) -> bool:
        """Whether process *pid* of this host is running."""
        proc = self.processes.get(pid)
        return proc is not None and proc.alive

    def monotonic(self) -> float:
        """Virtual seconds."""
        return self.clock.now

    def wall_time(self) -> float:
        """Virtual wall seconds."""
        return _WALL_EPOCH + self.clock.now

    def sleep(self, seconds: float) -> None:
        """Let virtual time pass."""
        self.clock.advance_to(self.clock.now + max(seconds, 0.0))

    def alive_pids(self) -> set[int]:
        """Every process of this host still running (descendants included)."""
        return {pid for pid, proc in self.processes.items() if proc.alive}


def _marker_file_status(text: str) -> int:
    """Evaluate a ``test -f 'A' || test -f 'B'`` readiness check: 0 once any named file exists."""
    return 0 if any(Path(path).is_file() for path in _TEST_F.findall(text)) else 1


_TEST_F = re.compile(r"test -f '([^']+)'")


# -- script builders --------------------------------------------------------------


def exits(after_s: float, code: int = 0, output: str = "") -> Script:
    """A script that prints *output* and exits with *code* after *after_s* seconds."""

    def script(proc: FakeProcess) -> None:
        proc.log(output)
        proc.after(after_s, lambda: proc.exit(code))

    return script


def serves(
    *,
    stop_after_s: float = 0.0,
    ignores: tuple[str, ...] = (),
    on_stop: Callable[[FakeProcess], None] | None = None,
    exits_on_stop: bool = True,
) -> Script:
    """A script that runs until stopped.

    On ``SIGINT`` (the default stop signal) it runs ``on_stop`` (for instance to flush a
    trace), then exits (status 0) after *stop_after_s* seconds. Signals named in
    ``ignores`` are ignored, as a server that traps them would. ``exits_on_stop=False`` is a
    server that flushes on the stop signal and then hangs, to be escalated.
    """

    def script(proc: FakeProcess) -> None:
        def stop() -> None:
            if on_stop is not None:
                on_stop(proc)
            if exits_on_stop:
                proc.after(stop_after_s, lambda: proc.exit(0))

        proc.on_signal("SIGINT", stop)
        for name in ignores:
            proc.on_signal(name, None)

    return script


def prints(text: str, status: int = 0) -> Probe:
    """A capability query that always answers *text* with *status*."""
    return lambda _argv: (status, text)


def always(status: int) -> Check:
    """A check that always exits with *status*."""
    return lambda: status


# -- a warm torch target ----------------------------------------------------------------


def torch_target(
    trace_json: bytes,
    *,
    gpu: bool = True,
    start_delay_s: float = 0.0,
    export_delay_s: float = 0.0,
    events: list[str] | None = None,
) -> Script:
    """A script that behaves as a process armed by the torch injection in ``signal`` mode.

    It speaks the injection's file protocol (``torch/inject/sitecustomize.py``): once started
    it writes ``<control>/ready`` (or ``<control>/unavailable`` when it has no GPU); SIGUSR1
    consumes ``<control>/next_window`` and, after ``start_delay_s``, records
    ``.window-<pid>-<n>.started`` plus ``window.started`` in that directory; SIGUSR2 writes
    ``<pid>-<n>.pt.trace.json.gz`` and ``window.exported`` after ``export_delay_s`` (and
    cancels a start still pending); SIGINT exits. Milestones are appended to *events*.
    That the real injection writes exactly these files is checked against real processes in
    ``tests/e2e/test_profiler_capture_processes_e2e.py``.
    """
    log = events if events is not None else []

    def script(proc: FakeProcess) -> None:
        control = Path(proc.env["VIBESYS_TORCH_PROFILE_CONTROL_DIR"])
        window_dir: list[Path] = []
        windows = itertools.count(1)
        number = [0]
        recording = [False]
        pending_start = [False]

        def announce() -> None:
            if gpu:
                (control / "ready").write_text("ready\n")
            else:
                (control / "unavailable").write_text("no GPU visible to torch\n")

        def begin() -> None:
            if not pending_start[0]:
                return
            pending_start[0] = False
            recording[0] = True
            directory = window_dir[0]
            (directory / f".window-{proc.pid}-{number[0]}.started").write_text("")
            (directory / "window.started").write_text("")
            log.append("profile.start")

        def on_usr1() -> None:
            control_file = control / "next_window"
            directory = Path(control_file.read_text().strip())
            control_file.unlink()
            window_dir[:] = [directory]
            number[0] = next(windows)
            pending_start[0] = True
            proc.after(start_delay_s, begin)

        def finish() -> None:
            directory = window_dir[0]
            trace = directory / f"{proc.pid}-{number[0]}.pt.trace.json.gz"
            trace.write_bytes(gzip.compress(trace_json))
            (directory / "window.exported").write_text("")
            recording[0] = False
            log.append("profile.export")

        def on_usr2() -> None:
            pending_start[0] = False
            if recording[0]:
                proc.after(export_delay_s, finish)

        proc.on_signal("SIGUSR1", on_usr1)
        proc.on_signal("SIGUSR2", on_usr2)
        proc.on_signal("SIGINT", lambda: proc.exit(0))
        proc.after(0.0, announce)

    return script


def injected_process(
    trace_json: bytes,
    *,
    gpu: bool = True,
    export_delay_s: float = 0.0,
    run_s: float | None = 0.5,
    events: list[str] | None = None,
) -> Script:
    """A script that behaves as a process the torch injection armed in its default mode.

    With a GPU it opens a window at once (``.window-<pid>-1.started`` under
    ``VIBESYS_TORCH_PROFILE_OUT_DIR``) and writes ``<pid>-1.pt.trace.json.gz`` once
    *export_delay_s* passed; without one it writes nothing. A *run_s* process exits after that
    long (never before its export, as the real injection exports at exit); ``run_s=None`` is a
    server that exports and exits on SIGINT.
    """
    log = events if events is not None else []

    def script(proc: FakeProcess) -> None:
        out_dir = Path(proc.env["VIBESYS_TORCH_PROFILE_OUT_DIR"])
        out_dir.mkdir(parents=True, exist_ok=True)
        exported = [not gpu]

        def export() -> None:
            (out_dir / f"{proc.pid}-1.pt.trace.json.gz").write_bytes(gzip.compress(trace_json))
            exported[0] = True
            log.append("profile.export")

        if gpu:
            (out_dir / f".window-{proc.pid}-1.started").write_text("")
            log.append("profile.start")

        def leave() -> None:
            if not exported[0]:
                proc.after(export_delay_s, export)
                proc.after(export_delay_s, lambda: proc.exit(0))
            else:
                proc.exit(0)

        if run_s is None:
            proc.on_signal("SIGINT", leave)
        else:
            proc.after(max(run_s, export_delay_s), leave)

    return script
