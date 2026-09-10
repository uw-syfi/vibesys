"""Run agentshim CLI commands inside an already-running VibeSys Docker sandbox.

Three pieces live here:

* :class:`DockerCommandExecutor` -- a ``docker exec`` transport built on the
  library's ``TransformingExecutor``. It carries no provider knowledge: it
  rewrites argv, forwards the environment entries its caller nominated, and
  lets agentshim own everything else.
* :func:`repair_workspace_ownership` -- returns bind-mounted workspace files to
  the host user after a container turn.
* :class:`CodexRolloutWatchdogExecutor` -- provider-behaviour compensation, not
  transport. A resumed ``codex exec ... --json`` run inside a container
  regularly finishes its work and writes the terminal events to its rollout
  file, then never exits, so the ``docker exec`` in front of it blocks until
  the turn budget expires. The watchdog reads the rollout, replays the
  completion into the stream, and stops the process. This stays in VibeSys
  until Codex resume inside containers is verified fixed upstream; it does not
  belong in agentshim, which models what a provider is documented to do.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from agentshim import (
    CommandRequest,
    CommandResult,
    HostCommandExecutor,
    TransformingExecutor,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from agentshim import CommandExecutor, CommandHandle, CommandStreamSink

#: Where a container turn runs when the request does not name a directory.
DEFAULT_CONTAINER_WORKDIR = "/workspace"

_OWNERSHIP_REPAIR_TIMEOUT_S = 120
_DOCKER_QUERY_TIMEOUT_S = 5


def _binary_in_container(name: str, env: Mapping[str, str]) -> str:
    """Resolve the provider binary inside the container, not on the host.

    The host running VibeSys usually has no copy of the CLI, so the bare name
    is the right ``argv[0]``: the container's ``PATH`` resolves it.
    """
    del env
    return name


class DockerCommandExecutor(TransformingExecutor):
    """Prefix every agentshim command with ``docker exec`` into one container.

    ``container_id_resolver`` is read once per request rather than captured at
    construction, so a sandbox that replaces its container (a GPU reselect, for
    instance) needs no new executor.

    The health check goes through the same transform, so ``<binary> --help`` is
    checked inside the container where the binary actually exists.
    """

    def __init__(
        self,
        container_id_resolver: Callable[[], str],
        *,
        workdir: str = DEFAULT_CONTAINER_WORKDIR,
        forward_env: Sequence[str] = (),
        inner: CommandExecutor | None = None,
    ) -> None:
        """Wrap *inner* (the local host by default) in ``docker exec``.

        *forward_env* names the environment variables that cross into the
        container; their values are read from each request, so a per-turn
        override still takes effect. Everything else stays outside.
        """
        self._container_id_resolver = container_id_resolver
        self._workdir = workdir
        self._forward_env = tuple(forward_env)
        super().__init__(
            inner if inner is not None else HostCommandExecutor(),
            self._to_docker_exec,
            find_binary=_binary_in_container,
        )

    def _forwarded_env(self, env: Mapping[str, str]) -> dict[str, str]:
        """Return the request environment entries that belong in the container.

        A container starts from its image's environment, not the host's. The
        environment agentshim assembles for a turn describes the *host*
        (``PATH``, ``HOME``, interpreter and toolchain locations, whatever a
        login shell exports), so forwarding entries by inspection would point
        the container CLI at directories that do not exist inside it and could
        smuggle host settings such as a model override past the container's own
        configuration. The caller that knows which variables are meant for the
        container names them instead.
        """
        return {key: env[key] for key in self._forward_env if key in env}

    def _to_docker_exec(self, request: CommandRequest) -> CommandRequest:
        """Rewrite one request into the equivalent ``docker exec`` invocation."""
        argv: list[str] = ["docker", "exec", "-i", "-w", request.cwd or self._workdir]
        for key, value in self._forwarded_env(request.env).items():
            argv += ["-e", f"{key}={value}"]
        argv.append(self._container_id_resolver())
        argv.extend(request.argv)
        return CommandRequest(
            argv=argv,
            stdin=request.stdin,
            cwd=None,
            # The transformed command is `docker` itself, running on the host:
            # it needs the host environment (PATH, DOCKER_HOST, certificates)
            # to reach the daemon at all.
            env=os.environ,
            timeout=request.timeout,
        )


def repair_workspace_ownership(container_id: str, *, uid: int, gid: int) -> None:
    """Return bind-mounted workspace files to the host user.

    CLI agents run as root in the editor container. Some editors replace files
    atomically, which leaves the replacement owned by root and can make the
    host-side checkpoint code unable to read it. Repair ownership before
    control returns to the framework rather than waiting until a later resume.

    Raises:
        RuntimeError: if the in-container ``chown`` sweep failed.
    """
    result = subprocess.run(  # noqa: S603  # tracked: #288
        [  # noqa: S607  # tracked: #288
            "docker",
            "exec",
            container_id,
            "find",
            "/workspace",
            "-xdev",
            "-user",
            "0",
            "-writable",
            "-exec",
            "chown",
            f"{uid}:{gid}",
            "{}",
            "+",
        ],
        capture_output=True,
        text=True,
        timeout=_OWNERSHIP_REPAIR_TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            "failed to restore writable Docker workspace ownership"
            + (f": {detail}" if detail else "")
        )


@dataclass(frozen=True)
class _CodexRolloutCompletion:
    """Terminal response recovered from a stalled resumed Codex rollout."""

    fingerprint: str
    message: str


_CODEX_RESUME_TERMINATION_SCRIPT = r"""
import os
import signal
import sys

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
    if "exec" not in argv or "resume" not in argv or thread_id not in argv:
        continue
    if not any(os.path.basename(part) == "codex" for part in argv):
        continue
    try:
        os.kill(int(entry), signal.SIGTERM)
    except ProcessLookupError:
        pass
"""


def _no_lines() -> list[str]:
    return []


@dataclass
class _WatchdogState:
    """What the watchdog thread observed, shared with the calling thread."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    stdout_lines: list[str] = field(default_factory=_no_lines)
    recovered_completion: _CodexRolloutCompletion | None = None
    stalled_after_exit: bool = False

    def record(self, line: str) -> None:
        """Keep a stdout line so the watchdog can learn the thread ID from it."""
        with self.lock:
            self.stdout_lines.append(line)

    def snapshot(self) -> list[str]:
        """Return a stable copy of the stdout seen so far."""
        with self.lock:
            return list(self.stdout_lines)


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

    #: Seconds between liveness checks while a Codex JSON run is in flight.
    tick_seconds = 5.0
    #: Seconds between rollout-file reads.
    rollout_poll_seconds = 15.0
    #: How long a terminal rollout state must hold before it is trusted.
    completion_grace_seconds = 30.0
    #: How long to wait for the run to end after the in-container SIGTERM.
    termination_grace_seconds = 5.0

    def __init__(
        self,
        inner: CommandExecutor,
        container_id_resolver: Callable[[], str],
        *,
        log: Callable[[str], None] = _discard,
    ) -> None:
        """Wrap *inner*, watching containers named by *container_id_resolver*."""
        self._inner = inner
        self._container_id_resolver = container_id_resolver
        self._log = log
        self._codex_rollout_paths: dict[str, str] = {}

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
        """Replay recovered output and clear the exit code the watchdog caused."""
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
        if state.stalled_after_exit:
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
                    state.recovered_completion = completion
                    _terminate_codex_resume(container_id, thread_id)
                    if not stopped.wait(self.termination_grace_seconds):
                        handle.kill()
                    return
            if not self._codex_process_alive(container_id, child_binary):
                state.stalled_after_exit = True
                handle.kill()
                return

    def _codex_process_alive(self, container_id: str, child_binary: str) -> bool:
        """Return whether the CLI this ``docker exec`` fronts is still running."""
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

    def _read_codex_rollout_completion(  # noqa: C901, PLR0912  # tracked: #288
        self,
        container_id: str,
        thread_id: str,
    ) -> _CodexRolloutCompletion | None:
        """Read stable terminal evidence from a resumed Codex rollout, if any."""
        rollout_path = self._codex_rollout_paths.get(thread_id)
        if rollout_path is None:
            located = subprocess.run(  # noqa: S603  # tracked: #288
                [  # noqa: S607  # tracked: #288
                    "docker",
                    "exec",
                    container_id,
                    "find",
                    "/root/.codex/sessions",
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

        last_lifecycle_index = -1
        last_lifecycle_type: str | None = None
        for index, event in enumerate(events):
            if event.get("type") != "event_msg":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            payload_type = payload.get("type")
            if payload_type in {"task_started", "task_complete"}:
                last_lifecycle_index = index
                last_lifecycle_type = cast("str", payload_type)
        if last_lifecycle_type != "task_complete":
            return None

        message: str | None = None
        for event in reversed(events[: last_lifecycle_index + 1]):
            if event.get("type") != "event_msg":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "agent_message":
                continue
            candidate_message = payload.get("message")
            if isinstance(candidate_message, str) and candidate_message:
                message = candidate_message
                break
        if message is None:
            return None

        fingerprint = events[last_lifecycle_index].get("timestamp")
        if not isinstance(fingerprint, str) or not fingerprint:
            return None
        return _CodexRolloutCompletion(fingerprint=fingerprint, message=message)


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
