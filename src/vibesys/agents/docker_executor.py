"""Compensate for one Codex container behavior that agentshim does not model.

A resumed ``codex exec ... --json`` run inside a container regularly finishes
its work and writes the terminal events to its rollout file, then never
exits, so the ``docker exec`` in front of it blocks until the turn budget
expires. :class:`CodexRolloutWatchdogExecutor` reads the rollout, replays the
completion into the stream, and stops the process. This stays in VibeSys
until Codex resume inside containers is verified fixed upstream; it does not
belong in agentshim, which models what a provider is documented to do.

The transport itself -- rewriting a command into ``docker exec`` -- is not
here: :func:`vibesys.agents.drivers.agentshim.confine_to_sandbox` wraps every
executor, host or container, through the sandbox's own ``wrap``.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from agentshim import CommandResult

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from agentshim import CommandExecutor, CommandHandle, CommandRequest, CommandStreamSink

_DOCKER_QUERY_TIMEOUT_S = 5


@dataclass(frozen=True)
class _CodexRolloutCompletion:
    """Terminal response recovered from a stalled resumed Codex rollout."""

    fingerprint: str
    message: str


# The container-side process argv is exactly the request.argv the sandbox's
# own ``wrap`` prepended ``docker exec ... <container>`` to, so this script's
# match criteria are written to agree with the host-side
# ``_is_codex_json_command``/``_codex_resume_thread_id`` predicate: the binary
# basename is ``codex``, ``exec`` and ``--json`` are both present, and the
# thread ID sits immediately after ``resume``. That precision is required here
# and not in ``_codex_process_alive`` because a container can carry more than
# one stale resumed Codex process at a time (that is the situation this
# watchdog exists to clean up), so termination must single out the exact
# thread instead of matching any Codex process in the container.
def _codex_resume_argv_matches(argv: list[str], thread_id: str) -> bool:
    """Return whether *argv* is the resumed Codex JSON run for *thread_id*.

    Shared verbatim with the container-side termination script (its source is
    embedded below), so the host and the container agree on what a resumed
    Codex process looks like. Inside a container the CLI is a node script, so
    the process argv reads ``node /usr/local/bin/codex exec resume ...`` while
    its native child reads ``.../codex exec resume ...``; both must match.
    """
    if len(argv) > 1 and os.path.basename(argv[0]) != "codex":  # noqa: PTH119  # tracked: #288
        argv = argv[1:]
    if not argv or os.path.basename(argv[0]) != "codex":  # noqa: PTH119  # tracked: #288
        return False
    if "exec" not in argv or "--json" not in argv:
        return False
    try:
        resume_index = argv.index("resume")
    except ValueError:
        return False
    return argv[resume_index + 1 : resume_index + 2] == [thread_id]


_CODEX_RESUME_TERMINATION_SCRIPT = (
    "from __future__ import annotations\n\nimport os\nimport signal\nimport sys\n\n"
    + inspect.getsource(_codex_resume_argv_matches)
    + r"""

thread_id = sys.argv[1]
own_pid = os.getpid()
for entry in os.listdir("/proc"):
    if not entry.isdigit() or int(entry) == own_pid:
        continue
    try:
        raw = open(f"/proc/{entry}/cmdline", "rb").read()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        continue
    argv = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
    if not _codex_resume_argv_matches(argv, thread_id):
        continue
    try:
        os.kill(int(entry), signal.SIGTERM)
    except ProcessLookupError:
        pass
"""
)


class _WatchdogState:
    """What the watchdog thread observed, shared with the calling thread.

    Every field is written on the watchdog's helper thread and read on the
    thread that called ``run``, so one lock guards all of them rather than the
    stdout buffer alone.
    """

    def __init__(self) -> None:
        """Start with nothing observed."""
        self._lock = threading.Lock()
        self._stdout_lines: list[str] = []
        self._recovered_completion: _CodexRolloutCompletion | None = None
        self._killed_by_watchdog = False

    def record(self, line: str) -> None:
        """Keep a stdout line so the watchdog can learn the thread ID from it."""
        with self._lock:
            self._stdout_lines.append(line)

    def snapshot(self) -> list[str]:
        """Return a stable copy of the stdout seen so far."""
        with self._lock:
            return list(self._stdout_lines)

    def recover(self, completion: _CodexRolloutCompletion) -> None:
        """Record the terminal response read out of the rollout file."""
        with self._lock:
            self._recovered_completion = completion

    def note_kill(self) -> None:
        """Record that the watchdog, not the CLI, is what ended this run."""
        with self._lock:
            self._killed_by_watchdog = True

    @property
    def recovered_completion(self) -> _CodexRolloutCompletion | None:
        """The rollout completion the watchdog recovered, if it recovered one."""
        with self._lock:
            return self._recovered_completion

    @property
    def killed_by_watchdog(self) -> bool:
        """Whether the watchdog stopped the run rather than letting it end."""
        with self._lock:
            return self._killed_by_watchdog


class _WatchdogSink:
    """Sink decorator: records stdout and hands the handle to the watchdog."""

    def __init__(
        self,
        inner: CommandStreamSink,
        state: _WatchdogState,
        on_started: Callable[[CommandHandle], None],
    ) -> None:
        self._inner = inner
        self._state = state
        self._on_started = on_started

    def started(self, handle: CommandHandle) -> None:
        """Forward the handle downstream, then arm the watchdog with it."""
        self._inner.started(handle)
        self._on_started(handle)

    def stdout(self, line: str) -> None:
        """Record and forward one stdout line."""
        self._state.record(line)
        self._inner.stdout(line)

    def stderr(self, line: str) -> None:
        """Forward one stderr line."""
        self._inner.stderr(line)


def _discard(message: str) -> None:
    """Default log sink: drop the message."""


class CodexRolloutWatchdogExecutor:
    """Rescue a resumed ``codex exec --json`` run that finished but never exited.

    Every other command passes straight through. For a Codex JSON run the
    executor watches the container from a helper thread while the inner
    executor blocks: it locates the rollout file for the thread, waits for a
    ``task_complete`` that stays stable across two polls, then stops the
    process. It also stops a ``docker exec`` that outlived the Codex process it
    was fronting.

    Recovered lines are replayed through the sink on the *calling* thread once
    the inner run has returned, because agentshim documents every sink callback
    as running on the thread that called ``run``. The parser is finished only
    after ``run`` returns, so the replay still reaches it.
    """

    # Each keyword argument is an independent documented timing knob; folding
    # them into a config object would break the constructors already calling
    # this with the current keyword spellings.
    def __init__(  # noqa: PLR0913
        self,
        inner: CommandExecutor,
        container_id_resolver: Callable[[], str],
        *,
        rollout_sessions_root: str,
        log: Callable[[str], None] = _discard,
        tick_seconds: float = 5.0,
        rollout_poll_seconds: float = 15.0,
        completion_grace_seconds: float = 30.0,
        termination_grace_seconds: float = 5.0,
    ) -> None:
        """Wrap *inner*, watching containers named by *container_id_resolver*.

        Args:
            inner: The transport a Codex JSON command ultimately runs through.
            container_id_resolver: Names the container to poll, read each time
                a Codex run needs it.
            rollout_sessions_root: Where a resumed thread's rollout file lives
                inside the container. The caller derives this from the
                sandbox's own ``HOME`` and the Codex provider's state
                directory, so nothing here hardcodes a home directory that a
                different base image could relocate.
            log: Sink for the watchdog's own progress lines; dropped by
                default.
            tick_seconds: Seconds between liveness checks while a Codex JSON
                run is in flight.
            rollout_poll_seconds: Seconds between rollout-file reads.
            completion_grace_seconds: How long a terminal rollout state must
                hold before it is trusted.
            termination_grace_seconds: How long to wait for the run to end
                after the in-container SIGTERM.
        """
        self._inner = inner
        self._container_id_resolver = container_id_resolver
        self._rollout_sessions_root = rollout_sessions_root
        self._log = log
        self._codex_rollout_paths: dict[str, str] = {}
        self.tick_seconds = tick_seconds
        self.rollout_poll_seconds = rollout_poll_seconds
        self.completion_grace_seconds = completion_grace_seconds
        self.termination_grace_seconds = termination_grace_seconds

    def find_binary(self, name: str, env: Mapping[str, str]) -> str:
        """Delegate binary lookup to the wrapped executor."""
        return self._inner.find_binary(name, env)

    def check_binary(self, path: str, env: Mapping[str, str], *, timeout: float) -> None:
        """Delegate the health check to the wrapped executor."""
        self._inner.check_binary(path, env, timeout=timeout)

    def run(self, request: CommandRequest, sink: CommandStreamSink) -> CommandResult:
        """Run *request*, watching it when it is a Codex JSON invocation."""
        if not _is_codex_json_command(request.argv):
            return self._inner.run(request, sink)

        state = _WatchdogState()
        stopped = threading.Event()
        watcher: threading.Thread | None = None

        def arm(handle: CommandHandle) -> None:
            nonlocal watcher
            watcher = threading.Thread(
                target=self._watch,
                args=(request.argv, handle, state, stopped),
                daemon=True,
            )
            watcher.start()

        try:
            result = self._inner.run(request, _WatchdogSink(sink, state, arm))
        finally:
            stopped.set()
            if watcher is not None:
                watcher.join(timeout=self.termination_grace_seconds)

        return self._apply_recovery(result, state, sink)

    def _apply_recovery(
        self,
        result: CommandResult,
        state: _WatchdogState,
        sink: CommandStreamSink,
    ) -> CommandResult:
        """Replay recovered output and clear only an exit code the watchdog caused.

        The exit code is cleared in exactly two cases: a rollout completion was
        recovered, so the turn demonstrably finished; or the watchdog itself
        stopped the run, so the code describes the watchdog's signal rather
        than the CLI's answer. A run that ended on its own keeps its code,
        including a Codex that crashed and left ``pgrep`` nothing to find.
        """
        completion = state.recovered_completion
        if completion is not None:
            replayed = _forward_codex_completion(completion, sink)
            self._log(
                "codex rollout watchdog: replayed the completed turn from the "
                "rollout file and stopped a `docker exec` that never exited; "
                "reporting the turn as successful"
            )
            return CommandResult(
                returncode=0,
                stdout=result.stdout + "".join(replayed),
                stderr=result.stderr,
            )
        if state.killed_by_watchdog:
            self._log(
                "codex rollout watchdog: the codex process is gone but its "
                "`docker exec` never exited; stopped it and reporting the turn "
                "as successful"
            )
            return CommandResult(returncode=0, stdout=result.stdout, stderr=result.stderr)
        return result

    def _watch(
        self,
        argv: Sequence[str],
        handle: CommandHandle,
        state: _WatchdogState,
        stopped: threading.Event,
    ) -> None:
        """Poll the container until the run ends or the watchdog stops it."""
        try:
            self._poll(argv, handle, state, stopped)
        except Exception as exc:  # noqa: BLE001  # tracked: #288
            # This runs on a helper thread: an escaping exception would vanish
            # and leave the turn to burn its whole budget with no explanation.
            self._log(f"codex rollout watchdog stopped after an unexpected error: {exc}")

    def _poll(
        self,
        argv: Sequence[str],
        handle: CommandHandle,
        state: _WatchdogState,
        stopped: threading.Event,
    ) -> None:
        container_id = self._container_id_resolver()
        child_binary = os.path.basename(argv[0]) if argv else ""  # noqa: PTH119  # tracked: #288
        thread_id = _codex_resume_thread_id(argv)
        next_poll = time.monotonic()
        fingerprint: str | None = None
        seen_at: float | None = None

        while not stopped.wait(self.tick_seconds):
            now = time.monotonic()
            if thread_id is None:
                thread_id = _codex_started_thread_id(state.snapshot())
            if thread_id is not None and now >= next_poll:
                next_poll = now + self.rollout_poll_seconds
                completion = self._read_codex_rollout_completion(container_id, thread_id)
                if completion is None:
                    fingerprint = None
                    seen_at = None
                elif completion.fingerprint != fingerprint:
                    fingerprint = completion.fingerprint
                    seen_at = now
                elif seen_at is not None and now - seen_at >= self.completion_grace_seconds:
                    state.recover(completion)
                    _terminate_codex_resume(container_id, thread_id)
                    if not stopped.wait(self.termination_grace_seconds):
                        state.note_kill()
                        handle.kill()
                    return
            if not self._codex_process_alive(container_id, child_binary):
                if stopped.is_set():
                    # The `docker exec` returned while this tick was in
                    # flight. Whatever code it carries is the run's own
                    # answer, including a nonzero one from a Codex that
                    # crashed; the watchdog has nothing to correct.
                    return
                state.note_kill()
                handle.kill()
                return

    def _codex_process_alive(self, container_id: str, child_binary: str) -> bool:
        """Return whether the CLI this ``docker exec`` fronts is still running.

        This deliberately matches by binary name alone, unlike the thread-exact
        predicate ``_CODEX_RESUME_TERMINATION_SCRIPT`` uses: this call answers
        only "has this ``docker exec``'s CLI stopped running", where a false
        positive (some other Codex process still alive) merely means the poll
        loop keeps waiting one more tick, which is cheap. Terminating the wrong
        process would not be.
        """
        try:
            check = subprocess.run(  # noqa: S603  # tracked: #288
                ["docker", "exec", container_id, "pgrep", "-f", child_binary],  # noqa: S607  # tracked: #288
                capture_output=True,
                timeout=_DOCKER_QUERY_TIMEOUT_S,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            # A busy or unreachable daemon is not evidence that the CLI died;
            # assume it is alive and re-check on the next tick.
            return True
        return check.returncode == 0

    def _read_codex_rollout_completion(
        self,
        container_id: str,
        thread_id: str,
    ) -> _CodexRolloutCompletion | None:
        """Read stable terminal evidence from a resumed Codex rollout, if any."""
        rollout_path = self._locate_codex_rollout(container_id, thread_id)
        if rollout_path is None:
            return None
        events = self._tail_codex_rollout_events(container_id, rollout_path)
        if events is None:
            return None
        return _scan_codex_rollout_completion(events)

    def _locate_codex_rollout(self, container_id: str, thread_id: str) -> str | None:
        """Find, and cache, the rollout file a resumed thread is writing to."""
        cached = self._codex_rollout_paths.get(thread_id)
        if cached is not None:
            return cached
        located = subprocess.run(  # noqa: S603  # tracked: #288
            [  # noqa: S607  # tracked: #288
                "docker",
                "exec",
                container_id,
                "find",
                self._rollout_sessions_root,
                "-type",
                "f",
                "-name",
                f"rollout-*-{thread_id}.jsonl",
                "-print",
            ],
            capture_output=True,
            text=True,
            timeout=_DOCKER_QUERY_TIMEOUT_S,
            check=False,
        )
        paths = [line for line in located.stdout.splitlines() if line]
        if located.returncode != 0 or not paths:
            return None
        rollout_path = max(paths)
        self._codex_rollout_paths[thread_id] = rollout_path
        return rollout_path

    def _tail_codex_rollout_events(
        self,
        container_id: str,
        rollout_path: str,
    ) -> list[dict[str, Any]] | None:
        """Read the tail of one rollout file and parse its JSON lines."""
        result = subprocess.run(  # noqa: S603  # tracked: #288
            ["docker", "exec", container_id, "tail", "-n", "512", rollout_path],  # noqa: S607  # tracked: #288
            capture_output=True,
            text=True,
            timeout=_DOCKER_QUERY_TIMEOUT_S,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        events: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events


def _scan_codex_rollout_completion(
    events: Sequence[dict[str, Any]],
) -> _CodexRolloutCompletion | None:
    """Scan already-parsed rollout events for a stable terminal state.

    The last lifecycle event has to be ``task_complete``: a rollout still
    running, or one whose most recent turn merely started, is not evidence.
    From there the completion carries the last ``agent_message`` written
    before that lifecycle event, and the lifecycle event's own timestamp as
    the fingerprint the watchdog waits to see hold steady across two polls.
    """
    last_lifecycle_index, last_lifecycle_type = _last_codex_lifecycle_event(events)
    if last_lifecycle_type != "task_complete":
        return None

    message = _last_codex_agent_message(events[: last_lifecycle_index + 1])
    if message is None:
        return None

    fingerprint = events[last_lifecycle_index].get("timestamp")
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    return _CodexRolloutCompletion(fingerprint=fingerprint, message=message)


def _last_codex_lifecycle_event(events: Sequence[dict[str, Any]]) -> tuple[int, str | None]:
    """Return the index and type of the last ``task_started``/``task_complete`` event."""
    last_index = -1
    last_type: str | None = None
    for index, event in enumerate(events):
        if event.get("type") != "event_msg":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("type")
        if payload_type in {"task_started", "task_complete"}:
            last_index = index
            last_type = cast("str", payload_type)
    return last_index, last_type


def _last_codex_agent_message(events: Sequence[dict[str, Any]]) -> str | None:
    """Return the last non-empty ``agent_message`` text among *events*, if any."""
    for event in reversed(events):
        if event.get("type") != "event_msg":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "agent_message":
            continue
        candidate_message = payload.get("message")
        if isinstance(candidate_message, str) and candidate_message:
            return candidate_message
    return None


def _is_codex_json_command(cmd: Sequence[str]) -> bool:
    """Return whether *cmd* is a machine-readable Codex exec invocation."""
    if not cmd or os.path.basename(cmd[0]) != "codex" or "--json" not in cmd:  # noqa: PTH119  # tracked: #288
        return False
    return "exec" in cmd


def _codex_resume_thread_id(cmd: Sequence[str]) -> str | None:
    """Return the validated thread ID for ``codex exec resume`` commands."""
    if not _is_codex_json_command(cmd):
        return None
    try:
        resume_index = list(cmd).index("resume")
        thread_id = cmd[resume_index + 1]
    except (ValueError, IndexError):
        return None
    if not re.fullmatch(r"[0-9a-fA-F-]{32,64}", thread_id):
        return None
    if not _codex_resume_argv_matches(list(cmd), thread_id):
        return None
    return thread_id


def _codex_started_thread_id(stdout_lines: Sequence[str]) -> str | None:
    """Recover a fresh invocation's thread ID from its streamed JSON."""
    for line in reversed(stdout_lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "thread.started":
            continue
        thread_id = event.get("thread_id")
        if isinstance(thread_id, str) and re.fullmatch(r"[0-9a-fA-F-]{32,64}", thread_id):
            return thread_id
    return None


def _forward_codex_completion(
    completion: _CodexRolloutCompletion,
    sink: CommandStreamSink,
) -> list[str]:
    """Send recovered output through agentshim's ordinary Codex parser."""
    synthetic_lines = [
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "vibesys-codex-rollout-watchdog",
                    "type": "agent_message",
                    "text": completion.message,
                },
            }
        )
        + "\n",
        json.dumps({"type": "turn.completed"}) + "\n",
    ]
    for line in synthetic_lines:
        sink.stdout(line)
    return synthetic_lines


def _terminate_codex_resume(container_id: str, thread_id: str) -> None:
    """Stop only the completed resumed Codex process inside this container."""
    subprocess.run(  # noqa: S603  # tracked: #288
        [  # noqa: S607  # tracked: #288
            "docker",
            "exec",
            container_id,
            "python3",
            "-c",
            _CODEX_RESUME_TERMINATION_SCRIPT,
            thread_id,
        ],
        capture_output=True,
        text=True,
        timeout=_DOCKER_QUERY_TIMEOUT_S,
        check=False,
    )
