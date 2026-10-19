"""Build the two Docker images a task runs from.

A task runs from an *agent image*: the shipped CLIs, helper tools, and a
non-root ``agent`` user layered on top of a *task image* (built from the
task's own Dockerfile, when it has one) or directly on a backend base image.
Task images stay pure and serve the evaluator; the agent layer is what a CLI
provider actually runs in, so a CLI version bump rebuilds only that top
layer.

Docker owns layer caching for both images; VibeSys invokes each build once
per launch and consumes the runnable manifest ID resolved from a local tag
derived from the build's own inputs, so an unchanged task or agent selection
retags rather than re-creates an image, and launches do not leave behind an
ever-growing set of local tags. Default provenance attestations are disabled
because their generated metadata otherwise changes the manifest ID when all
filesystem layers match.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import queue
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import JsonValue, TypeAdapter

from vs_agent._build_context import DOCKERIGNORE, build_context_inputs
from vs_agent.agent_feature import AGENT_USER, AGENT_WORKDIR, agent_feature
from vs_decode.api import decode_json
from vs_sandbox.api import FeatureImageBuildError, FinalImage, build_feature_image
from vs_sim.api import (
    Clock,
    Lines,
    OsThreads,
    OutputStream,
    PipedProcess,
    PipedProcessLauncher,
    ProcessTimeoutError,
    StreamingCommand,
    SubprocessPipedProcessLauncher,
    Threads,
    tee,
)

if TYPE_CHECKING:
    import io
    from collections.abc import Callable, Sequence

    from vs_sandbox.api import FeatureRef
    from vs_sim.api import OutputSink, Worker

DEFAULT_IMAGE_BUILD_TIMEOUT_SECONDS = 1200.0
"""Default budget for one launch's image builds, summed over every Docker command."""

_DEFAULT_BUILD_TIMEOUT_SECONDS = DEFAULT_IMAGE_BUILD_TIMEOUT_SECONDS
_DEFAULT_PUSH_TIMEOUT_SECONDS = 1200.0
_DEFAULT_VERIFY_TIMEOUT_SECONDS = 60.0
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_DIAGNOSTIC_LIMIT = 1000
DOCKER_OUTPUT_TAIL_CHARACTERS = 64 * 1024
"""Maximum characters retained from each Docker command output stream."""

_OUTPUT_READ_CHARACTERS = 16 * 1024

_PROCESS_SETTLE_TIMEOUT_SECONDS = 5.0
# After a normally exited leader, leave half of the one final cleanup budget
# for killing inherited writers and settling the readers they kept open.
_DRAIN_GRACE_FRACTION = 0.5
TASK_IMAGE_REPOSITORY = "vibesys-task-build"
"""Repository of the task image's local tag, ``<owner>-<inputs>``.

``<owner>`` is keyed to the task directory's absolute path, so two tasks (or
two checkouts, or two users of one daemon) never share a tag prefix.
``<inputs>`` is a digest of the build context, the Dockerfile, and the base
image argument, so an unchanged task retags the same image and a changed one
gets a new tag. A build prunes only its own owner's other tags.
"""
_TASK_IMAGE_REPOSITORY = TASK_IMAGE_REPOSITORY
#: Repository of the local tag that names a bare image ID so BuildKit can
#: resolve it in a ``FROM`` line. Keyed by the full ID, so re-tagging is a
#: no-op and the agent layer's cache key stays stable.
_BASE_IMAGE_REPOSITORY = "vibesys-base"
AGENT_IMAGE_REPOSITORY = "vibesys-agent-build"
"""The local repository every agent image build is tagged under (one tag per input digest)."""
#: A BuildKit progress line that starts a Dockerfile step: ``#7 [ 2/14] RUN ...``,
#: or ``#7 [stage 2/14] RUN ...`` in a named stage of a multi-stage build.
_BUILD_STEP = re.compile(r"^#\d+ \[(?:\S+ )?\s*(\d+)/(\d+)\] (.+)$")
#: BuildKit's last phase: it writes the layers out and unpacks them into the image store, which
#: for a large base image takes minutes after the last Dockerfile step is done.
_BUILD_EXPORT = re.compile(r"^#\d+ exporting to image")
#: The label of the observation that says the Dockerfile steps are done and the export runs.
_EXPORT_LABEL = "exporting to image"
#: Longest step label reported; a ``RUN`` line can be a whole shell script.
_STEP_LABEL_LIMIT = 200
#: Characters of a stopped build's output kept in its timeout error.
_TIMEOUT_OUTPUT_CHARACTERS = 4000

#: Where a pushed agent image lives. GHCR because the repository is on GitHub
#: and it is cloud-neutral for SkyPilot's arbitrary infra targets. Runs
#: reference the resulting digest, never this repository's moving tags.
DEFAULT_AGENT_IMAGE_REGISTRY = "ghcr.io/uw-syfi/vibesys-agent"

#: Length of the content-derived tag `push_agent_image` pushes under. Not a
#: security boundary, just short enough to read in logs while still keying
#: the tag off the image content, so re-pushing the same image ID reuses the
#: same tag and Docker's own layer-presence check makes the push itself a
#: fast no-op.
_SHORT_ID_LENGTH = 12


class TaskImageBuildError(RuntimeError):
    """Raised when Docker cannot produce a valid immutable image."""

    @classmethod
    def dockerfile_not_regular(cls, dockerfile: Path) -> TaskImageBuildError:
        """Describe a task Dockerfile that is missing or is a symlink."""
        return cls(f"Task Dockerfile must be a regular file: {dockerfile}")

    @classmethod
    def build_failed(
        cls, target: _BuildTarget, returncode: int, details: str
    ) -> TaskImageBuildError:
        """Describe a Docker build failure and its diagnostic output."""
        return cls(
            f"Could not build {target.image_label} from {target.source} "
            f"(exit {returncode}): {details[:_DIAGNOSTIC_LIMIT]}"
        )

    @classmethod
    def inspect_failed(
        cls,
        target: _BuildTarget,
        image_tag: str,
        returncode: int,
        details: str,
    ) -> TaskImageBuildError:
        """Describe a failed lookup of the built image's immutable ID."""
        return cls(
            f"Could not resolve runnable {target.image_label} {image_tag} built from "
            f"{target.source} (exit {returncode}): "
            f"{details[:_DIAGNOSTIC_LIMIT]}"
        )

    @classmethod
    def invalid_runnable_id(
        cls, image_label: str, source: str, displayed: str
    ) -> TaskImageBuildError:
        """Describe Docker returning a value that is not an immutable image ID."""
        return cls(
            f"Docker returned an invalid runnable {image_label} ID for {source}: {displayed!r}"
        )

    @classmethod
    def base_tag_failed(
        cls, image_id: str, tag: str, returncode: int, details: str
    ) -> TaskImageBuildError:
        """Describe Docker failing to give a base image ID a resolvable local tag."""
        return cls(
            f"Could not tag base image {image_id} as {tag} "
            f"(exit {returncode}): {details[:_DIAGNOSTIC_LIMIT]}"
        )

    @classmethod
    def docker_not_found(cls, action: str, target: _BuildTarget) -> TaskImageBuildError:
        """Describe Docker being unavailable for an image build action."""
        return cls(f"Docker was not found while {action} {target.image_label}: {target.source}")


class ImageBuildTimeoutError(TaskImageBuildError):
    """Raised when an image build outlives its budget and its Docker process was stopped.

    ``budget_seconds`` is the whole launch's build budget, not one command's.
    ``step`` names the Dockerfile step that was running when time ran out (empty
    when Docker had announced none), and ``output`` is the tail of what the build
    printed, so a stalled network fetch shows up in the error instead of
    vanishing with the killed process.
    """

    def __init__(
        self, budget_seconds: float, action: str, subject: str, output: str, step: str
    ) -> None:
        """Describe the expired budget, the running step, and the captured output tail."""
        self.budget_seconds = budget_seconds
        self.step = step
        self.output = output
        running = f"; running {step}" if step else ""
        tail = f"\nLast build output:\n{output}" if output else ""
        super().__init__(
            f"Docker build budget of {budget_seconds:g} seconds ran out while "
            f"{action} {subject}{running}{tail}"
        )


class ImagePushError(RuntimeError):
    """Raised when an agent image cannot be pushed to, or verified in, a registry."""

    @classmethod
    def run_environment_push_failed(
        cls, image_id: str, backend_label: str, error: Exception
    ) -> ImagePushError:
        """Add the selected environment to an image push failure."""
        return cls(
            f"could not push or verify the agent image {image_id} in the "
            f"registry for a {backend_label} run: {error}"
        )

    @classmethod
    def tag_failed(cls, image_id: str, tag: str, returncode: int, details: str) -> ImagePushError:
        """Describe Docker failing to tag an agent image for publishing."""
        return cls(
            f"Could not tag agent image {image_id} as {tag} "
            f"(exit {returncode}): {details[:_DIAGNOSTIC_LIMIT]}"
        )

    @classmethod
    def push_failed(cls, tag: str, returncode: int, details: str) -> ImagePushError:
        """Describe a registry push failure and the required login step."""
        return cls(
            f"Could not push agent image {tag} (exit {returncode}): "
            f"{details[:_DIAGNOSTIC_LIMIT]}. Authenticate first with "
            "`docker login ghcr.io` using a token with `write:packages` scope "
            "(`GITHUB_TOKEN` in CI)."
        )

    @classmethod
    def digest_missing(cls, tag: str, details: str) -> ImagePushError:
        """Describe a push whose Docker inspection returned no repo digest."""
        return cls(
            f"Pushed {tag} but Docker returned no repo digest for it: {details[:_DIAGNOSTIC_LIMIT]}"
        )

    @classmethod
    def repository_digest_missing(cls, tag: str, repository: str, output: str) -> ImagePushError:
        """Describe Docker output without a digest for the requested repository."""
        return cls(
            f"Docker returned no repo digest for {tag} under {repository}: "
            f"{output[:_DIAGNOSTIC_LIMIT]!r}"
        )

    @classmethod
    def digest_not_visible(cls, pushed: str) -> ImagePushError:
        """Describe a pushed digest not yet visible to the registry."""
        return cls(
            f"Pushed {pushed} but the registry did not report it as present "
            "immediately afterward; retry, or check registry availability."
        )

    @classmethod
    def docker_not_found(cls, action: str) -> ImagePushError:
        """Describe Docker being unavailable during image publication."""
        return cls(f"Docker was not found while {action} the agent image")

    @classmethod
    def docker_timed_out(cls, timeout: float, action: str) -> ImagePushError:
        """Describe a registry command that exceeded its timeout."""
        return cls(f"Docker timed out after {timeout:g} seconds while {action} the agent image")


@dataclass(frozen=True)
class ImageBuildProgress:
    """How far one image build has come: ``done`` of ``total`` Dockerfile steps.

    ``label`` is the step now running, as Docker names it (``RUN npm install
    ...``), cut to a bounded length. Observations of one build only move
    forward; a build of two images (task, then agent) reports each in turn.
    When every step is done and Docker writes the image out (minutes, for a
    large base), ``done`` equals ``total`` and ``label`` is ``exporting to
    image``.
    """

    done: int
    total: int
    label: str


type BuildProgressSink = Callable[[ImageBuildProgress], None]
"""Where an image build reports its progress."""


class _StepTracker:
    """Turn a build's BuildKit progress lines into forward-moving step observations.

    It reads the lines of the build's raw output (through :class:`~vs_sim.api.Lines`,
    teed beside the caller's sink), so parsing never decides which bytes reach the
    caller. BuildKit writes its progress to stderr; stdout lines are not steps.
    """

    def __init__(self, sink: BuildProgressSink | None = None) -> None:
        self._sink = sink
        self._done = -1
        self._total = 0
        self._exporting = False
        self._running = ""

    @property
    def running(self) -> str:
        """The latest Dockerfile step Docker announced, such as ``[2/14] RUN ...``."""
        return self._running

    def line(self, stream: OutputStream, text: str) -> None:
        """Observe one line of the build's output."""
        if stream is not OutputStream.STDERR:
            return
        if self._total and not self._exporting and _BUILD_EXPORT.match(text):
            # Every step is done: all of ``total``, now writing the image out.
            self._exporting = True
            self._done = self._total
            self._running = _EXPORT_LABEL
            if self._sink is not None:
                self._sink(
                    ImageBuildProgress(done=self._total, total=self._total, label=_EXPORT_LABEL)
                )
            return
        match = _BUILD_STEP.match(text)
        if match is None:
            return
        step, total = int(match.group(1)), int(match.group(2))
        if not 1 <= step <= total:
            return
        done = step - 1
        if done <= self._done:
            # A step re-announced (``CACHED``, a parallel stage) is not progress.
            return
        self._done = done
        self._total = total
        label = match.group(3)[:_STEP_LABEL_LIMIT]
        self._running = f"step {step}/{total}: {label}"
        if self._sink is not None:
            self._sink(ImageBuildProgress(done=done, total=total, label=label))


@dataclass(frozen=True)
class _BuildTarget:
    """What an image is built from, where Docker runs, and how to tag and name it.

    Bundled so the build/inspect/error-reporting helpers below stay under the
    project's argument-count limit. ``source`` is the human-readable origin
    (a Dockerfile path, a ``devcontainer.json`` path, or an image ID) that
    error messages name; ``context`` is the directory Docker commands run in.
    """

    source: str
    context: Path
    image_repository: str
    image_label: str


@dataclass(frozen=True)
class _BuiltImage:
    """A built image: its unique local tag and its immutable ID.

    The tag is what a later ``FROM`` or ``--build-arg`` must name, because
    BuildKit resolves ``sha256:<id>`` as a registry reference and fails. The ID
    is what gets recorded for reproducibility.
    """

    tag: str
    image_id: str


@dataclass(frozen=True)
class DockerCommandResult:
    """Exit status and bounded diagnostic tails from one Docker command.

    ``stdout`` and ``stderr`` contain at most
    :data:`DOCKER_OUTPUT_TAIL_CHARACTERS` characters each. The corresponding
    omitted counts make truncation explicit without placing a marker in
    machine-readable command output.
    """

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    stdout_omitted_characters: int = 0
    stderr_omitted_characters: int = 0

    def __post_init__(self) -> None:
        """Reject inconsistent results at the process boundary."""
        if self.stdout_omitted_characters < 0 or self.stderr_omitted_characters < 0:
            message = "Docker output omitted-character counts must be nonnegative"
            raise ValueError(message)
        if (
            len(self.stdout) > DOCKER_OUTPUT_TAIL_CHARACTERS
            or len(self.stderr) > DOCKER_OUTPUT_TAIL_CHARACTERS
        ):
            message = "Docker command output exceeds the retained-tail limit"
            raise ValueError(message)

    @property
    def stdout_truncated(self) -> bool:
        """Whether characters before the retained stdout tail were discarded."""
        return self.stdout_omitted_characters > 0

    @property
    def stderr_truncated(self) -> bool:
        """Whether characters before the retained stderr tail were discarded."""
        return self.stderr_omitted_characters > 0

    def stdout_diagnostic(self, fallback: str, *, limit: int = _DIAGNOSTIC_LIMIT) -> str:
        """Render bounded stdout with an explicit truncation marker."""
        return _diagnostic_text(
            self.stdout,
            self.stdout_omitted_characters,
            fallback=fallback,
            limit=limit,
        )

    def failure_diagnostic(self, fallback: str, *, limit: int = _DIAGNOSTIC_LIMIT) -> str:
        """Prefer stderr, then stdout, as a bounded command-failure diagnostic."""
        if self.stderr.strip() or self.stderr_truncated:
            return _diagnostic_text(
                self.stderr,
                self.stderr_omitted_characters,
                fallback=fallback,
                limit=limit,
            )
        return self.stdout_diagnostic(fallback, limit=limit)


class LegacyDockerCommandResult(Protocol):
    """Structural contract of older ``CompletedProcess``-returning runners."""

    @property
    def args(self) -> Sequence[str] | str:
        """The executed argument vector."""
        ...

    @property
    def returncode(self) -> int:
        """The process exit status."""
        ...

    @property
    def stdout(self) -> str | None:
        """Captured stdout, or ``None`` when capture was disabled."""
        ...

    @property
    def stderr(self) -> str | None:
        """Captured stderr, or ``None`` when capture was disabled."""
        ...


type DockerBuildResult = DockerCommandResult | LegacyDockerCommandResult
"""Image-runner result, including legacy injected runners during migration."""


def _bounded_result(result: DockerBuildResult) -> DockerCommandResult:
    """Adapt an existing ``CompletedProcess`` runner to the bounded result contract."""
    if isinstance(result, DockerCommandResult):
        return result
    raw_args = result.args
    args = (raw_args,) if isinstance(raw_args, str) else tuple(raw_args)
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    stdout_omitted = max(0, len(stdout) - DOCKER_OUTPUT_TAIL_CHARACTERS)
    stderr_omitted = max(0, len(stderr) - DOCKER_OUTPUT_TAIL_CHARACTERS)
    return DockerCommandResult(
        args=args,
        returncode=result.returncode,
        stdout=stdout[-DOCKER_OUTPUT_TAIL_CHARACTERS:],
        stderr=stderr[-DOCKER_OUTPUT_TAIL_CHARACTERS:],
        stdout_omitted_characters=stdout_omitted,
        stderr_omitted_characters=stderr_omitted,
    )


def _diagnostic_text(text: str, omitted: int, *, fallback: str, limit: int) -> str:
    """Render one retained tail within *limit*, naming discarded output."""
    if limit <= 0:
        message = "Docker diagnostic limit must be positive"
        raise ValueError(message)
    retained = text.strip()
    if not retained and omitted == 0:
        return fallback[:limit]
    if omitted == 0:
        return retained[-limit:]
    marker = f"[... {omitted} earlier Docker output characters truncated ...]\n"
    if len(marker) >= limit:
        return marker[:limit]
    return marker + retained[-(limit - len(marker)) :]


@dataclass(frozen=True)
class _RetainedOutput:
    """One bounded stream suffix and the size of its discarded prefix."""

    text: str
    omitted_characters: int


class _OutputTail:
    """Drain arbitrary text chunks while retaining a fixed-size suffix."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._parts: deque[str] = deque()
        self._retained_characters = 0
        self._omitted_characters = 0

    def append(self, text: str) -> None:
        """Retain the newest characters from *text*."""
        self._parts.append(text)
        self._retained_characters += len(text)
        excess = self._retained_characters - self._limit
        while excess > 0:
            oldest = self._parts[0]
            if len(oldest) <= excess:
                self._parts.popleft()
                removed = len(oldest)
            else:
                self._parts[0] = oldest[excess:]
                removed = excess
            self._retained_characters -= removed
            self._omitted_characters += removed
            excess -= removed

    def result(self) -> _RetainedOutput:
        """Return the retained suffix and number of earlier characters discarded."""
        return _RetainedOutput("".join(self._parts), self._omitted_characters)


class DockerBuildRunner(Protocol):
    """Injectable process boundary for a Docker image build."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        output: OutputSink,
    ) -> DockerBuildResult:
        """Run one Docker command without a shell.

        Every chunk the command writes to stdout or stderr is written to
        ``output`` while the command runs, in order per stream, before this
        returns or raises. The result keeps an independently bounded tail of
        both streams, whatever ``output`` is.
        """
        ...


class _GuardedSink:
    """Forward drained chunks to the caller's sink, keeping its first failure for later.

    By the ``OutputSink`` contract a sink never raises (a log stream degrades instead),
    so a failure here is a programming error in the sink, which aborts the build. The
    guard only decides how: a drain keeps reading after the sink fails, since a drain
    that stopped would leave the child blocked on a full pipe until its timeout, and the
    failure is raised once both pipes have drained. The sink receives nothing after it.
    """

    def __init__(self, sink: OutputSink, errors: queue.SimpleQueue[Exception]) -> None:
        self._sink: OutputSink | None = sink
        self._errors = errors

    def write(self, stream: OutputStream, data: bytes) -> None:
        sink = self._sink
        if sink is None:
            return
        try:
            sink.write(stream, data)
        except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-994700 [BLE001]; output sink failures are reported after both process pipes have been drained, so a failing sink cannot deadlock the child.
            self._errors.put(exc)
            self._sink = None


@dataclass
class _DockerExecution:
    """Resources owned by one started Docker command."""

    process: PipedProcess
    streams: tuple[io.TextIOBase, io.TextIOBase]
    workers: list[Worker]


@dataclass(frozen=True)
class _Settlement:
    """Whether cleanup stopped every worker, plus cleanup failures to annotate."""

    workers_stopped: bool
    errors: tuple[BaseException, ...]


def _cleanup_attempt[T](
    operation: Callable[[], T],
    errors: list[BaseException],
    *,
    on_failure: T,
) -> T:
    """Run one cleanup operation, recording every failure so later steps still run."""
    try:
        return operation()
    except BaseException as exc:  # noqa: BLE001  # lint-waiver: LW-010245 [BLE001]; cleanup must continue through cancellation and interpreter-level failures so an injected lifecycle effect cannot replace the primary process failure or leak later-owned resources.
        errors.append(exc)
        return on_failure


class SubprocessDockerBuildRunner:
    """Run Docker directly while continuously draining bounded diagnostics."""

    def __init__(
        self,
        streaming: StreamingCommand | None = None,
        *,
        threads: Threads | None = None,
        processes: PipedProcessLauncher | None = None,
        cleanup_timeout: float = _PROCESS_SETTLE_TIMEOUT_SECONDS,
    ) -> None:
        """Use OS effects by default, retaining the positional streaming test seam.

        An explicitly injected legacy ``streaming`` command runs every call.
        Production execution uses the bounded piped-process boundary.
        """
        if not math.isfinite(cleanup_timeout) or cleanup_timeout < 0:
            message = "Docker process cleanup timeout must be finite and nonnegative"
            raise ValueError(message)
        self._threads = threads or OsThreads()
        self._processes = processes or SubprocessPipedProcessLauncher()
        self._streaming = streaming
        self._cleanup_timeout = cleanup_timeout

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        output: OutputSink,
    ) -> DockerCommandResult:
        """Run one command with continuously drained, bounded output.

        The pipes are read as text, so ``output`` receives each chunk as UTF-8 of
        the decoded text: an undecodable byte arrives as U+FFFD.
        """
        normalized = tuple(argv)
        if self._streaming is not None:
            result = self._streaming.run(
                normalized, cwd=cwd, timeout_seconds=timeout, output=output
            )
            return _bounded_result(result)
        process = self._processes.start(normalized, cwd=cwd)
        stdout_tail = _OutputTail(DOCKER_OUTPUT_TAIL_CHARACTERS)
        stderr_tail = _OutputTail(DOCKER_OUTPUT_TAIL_CHARACTERS)
        drain_errors: queue.SimpleQueue[Exception] = queue.SimpleQueue()
        execution = self._start_drains(
            process,
            stdout_tail,
            stderr_tail,
            drain_errors,
            output,
        )
        try:
            returncode = process.wait(timeout=timeout)
        except BaseException as failure:
            deadline = self._threads.now() + self._cleanup_timeout
            self._note_settlement(failure, self._settle(execution, deadline, kill=True))
            if isinstance(failure, ProcessTimeoutError):
                # The process is gone; what it printed is the only clue to what stalled.
                failure.output = stdout_tail.result().text.encode()
                failure.stderr = stderr_tail.result().text.encode()
            raise
        cleanup_started = self._threads.now()
        deadline = cleanup_started + self._cleanup_timeout
        drain_deadline = cleanup_started + self._cleanup_timeout * _DRAIN_GRACE_FRACTION
        drain_settlement = self._join_drains_until(execution.workers, drain_deadline)
        if not drain_settlement.workers_stopped:
            settlement = self._settle(
                execution,
                deadline,
                kill=True,
                prior_errors=drain_settlement.errors,
            )
            message = "Docker output pipes did not settle after the process exited"
            error = RuntimeError(message)
            self._note_settlement(error, settlement)
            raise error
        close_errors = self._close_streams(execution.streams)
        if close_errors:
            raise close_errors[0]
        if not drain_errors.empty():
            raise drain_errors.get()
        captured_stdout = stdout_tail.result()
        captured_stderr = stderr_tail.result()
        return DockerCommandResult(
            args=normalized,
            returncode=returncode,
            stdout=captured_stdout.text,
            stderr=captured_stderr.text,
            stdout_omitted_characters=captured_stdout.omitted_characters,
            stderr_omitted_characters=captured_stderr.omitted_characters,
        )

    def _start_drains(
        self,
        process: PipedProcess,
        stdout_tail: _OutputTail,
        stderr_tail: _OutputTail,
        drain_errors: queue.SimpleQueue[Exception],
        output: OutputSink,
    ) -> _DockerExecution:
        """Start both drains, cleaning up the process if either worker cannot start."""
        sink = _GuardedSink(output, drain_errors)

        def drain(stream: io.TextIOBase, name: OutputStream, tail: _OutputTail) -> None:
            try:
                while chunk := stream.readline(_OUTPUT_READ_CHARACTERS):
                    tail.append(chunk)
                    sink.write(name, chunk.encode())
            except (OSError, UnicodeError, ValueError) as exc:
                drain_errors.put(exc)

        execution = _DockerExecution(process, (process.stdout, process.stderr), [])
        try:
            execution.workers.append(
                self._threads.spawn(
                    lambda: drain(process.stdout, OutputStream.STDOUT, stdout_tail),
                    name="docker-stdout",
                    daemon=True,
                )
            )
            execution.workers.append(
                self._threads.spawn(
                    lambda: drain(process.stderr, OutputStream.STDERR, stderr_tail),
                    name="docker-stderr",
                    daemon=True,
                )
            )
        except BaseException as failure:
            deadline = self._threads.now() + self._cleanup_timeout
            self._note_settlement(failure, self._settle(execution, deadline, kill=True))
            raise
        return execution

    def _settle(
        self,
        execution: _DockerExecution,
        deadline: float,
        *,
        kill: bool,
        prior_errors: Sequence[BaseException] = (),
    ) -> _Settlement:
        """Settle one execution without allowing cleanup failures to skip later cleanup."""
        errors = list(prior_errors)
        if kill:
            _cleanup_attempt(execution.process.kill_group, errors, on_failure=None)
        _cleanup_attempt(execution.process.cancel_output, errors, on_failure=None)
        _cleanup_attempt(
            lambda: execution.process.wait(timeout=max(0.0, deadline - self._threads.now())),
            errors,
            on_failure=None,
        )
        worker_settlement = self._join_drains_until(execution.workers, deadline)
        errors.extend(worker_settlement.errors)
        errors.extend(self._close_streams(execution.streams))
        return _Settlement(worker_settlement.workers_stopped, tuple(errors))

    @staticmethod
    def _close_streams(streams: Sequence[io.TextIOBase]) -> tuple[BaseException, ...]:
        """Close every owned reader even when closing one reader fails."""
        errors: list[BaseException] = []
        for stream in streams:
            _cleanup_attempt(stream.close, errors, on_failure=None)
        return tuple(errors)

    @staticmethod
    def _note_settlement(failure: BaseException, settlement: _Settlement) -> None:
        """Attach cleanup evidence without replacing the triggering exception."""
        for error in settlement.errors:
            failure.add_note(f"Docker cleanup also raised {type(error).__name__}")
        if not settlement.workers_stopped:
            failure.add_note("Docker output drain workers did not settle before cleanup expired")

    def _join_drains_until(
        self,
        workers: Sequence[Worker],
        deadline: float,
    ) -> _Settlement:
        """Join output drains against one shared deadline."""
        errors: list[BaseException] = []
        for worker in workers:
            _cleanup_attempt(
                lambda worker=worker: worker.join(max(0.0, deadline - self._threads.now())),
                errors,
                on_failure=None,
            )
        workers_stopped = not errors
        for worker in workers:
            if _cleanup_attempt(worker.is_alive, errors, on_failure=True):
                workers_stopped = False
        return _Settlement(workers_stopped, tuple(errors))


# lint-waiver: LW-731409 [PLR0913]; the Dockerfile, base, runner, budget, progress sink, output
# > sink and the clock that measures the budget each change one build independently; a settings
# > object would only rename the keywords agent_image already passes the same way.
def build_task_image(  # noqa: PLR0913
    dockerfile_path: Path,
    *,
    base_image: str | None = None,
    command_runner: DockerBuildRunner | None = None,
    timeout: float = _DEFAULT_BUILD_TIMEOUT_SECONDS,
    on_progress: BuildProgressSink | None = None,
    output: OutputSink,
    clock: Clock,
) -> str:
    """Build a task Dockerfile and return its immutable Docker image ID.

    The task directory is the complete Docker build context. Candidate source,
    Git metadata, and other project inputs therefore cannot be copied into the
    image. The local tag Docker builds to is keyed to a digest of the build
    context, the Dockerfile, and ``base_image``, so an unchanged task retags
    the same image instead of adding another local tag, and Docker's own
    layer cache answers the rebuild without redoing any step. The tag is a
    local reachability anchor: containerd's image store cannot run the stable
    config digest emitted by ``--iidfile`` when an otherwise untagged image
    has no retained manifest reference. Every call prunes this repository's
    other local tags, so a changed task's old tag does not linger. Default
    provenance attestations are disabled because their generated metadata
    otherwise changes the manifest ID when all filesystem layers match.

    ``base_image``, when given, is passed as ``--build-arg BASE_IMAGE=...``,
    so a task Dockerfile may declare ``ARG BASE_IMAGE`` and
    ``FROM ${BASE_IMAGE}`` to build on top of the backend's base image instead
    of naming its own. A task Dockerfile with its own ``FROM`` line ignores an
    unused build arg, so this is safe to pass unconditionally from a caller
    that does not know whether the task Dockerfile uses it.

    ``on_progress``, when given, receives the build's step observations.
    ``output`` receives every chunk each Docker command of the build writes, as
    it is written; the step observations are parsed from the same bytes.

    ``timeout`` is the budget for the whole build, not for one Docker command.
    When it runs out, the running Docker process group is killed and
    :class:`ImageBuildTimeoutError` carries the step and output tail.
    ``clock`` measures the budget.
    """
    dockerfile = _validated_task_dockerfile(dockerfile_path, timeout)
    runner = command_runner or SubprocessDockerBuildRunner()
    return _build_task_image(
        dockerfile,
        base_image=base_image,
        build=_BuildRun(
            runner=runner, timeout=timeout, on_progress=on_progress, output=output, clock=clock
        ),
    ).image_id


def _validated_task_dockerfile(dockerfile_path: Path, timeout: float) -> Path:
    """Return the task Dockerfile's absolute path, or raise if it is unusable."""
    # Keep the lexical parent as the build context. Resolving the Dockerfile
    # itself could silently widen the context to a symlink target elsewhere.
    dockerfile = dockerfile_path.expanduser().absolute()
    if not dockerfile.is_file() or dockerfile.is_symlink():
        raise TaskImageBuildError.dockerfile_not_regular(dockerfile)
    if timeout <= 0:
        message = "task image build timeout must be positive"
        raise ValueError(message)
    return dockerfile


def task_image_reference(dockerfile_path: Path, *, base_image: str | None) -> str:
    """Return the local tag :func:`build_task_image` builds *dockerfile_path* to.

    The tag is ``<owner>-<inputs>``: ``<owner>`` keys the task directory's
    location (it scopes pruning only), and ``<inputs>`` is a digest of every
    build input (the Dockerfile, the context files ``.dockerignore`` keeps,
    ``base_image``), read now, without Docker. A run records it so a resume can
    tell unchanged inputs from changed ones with :func:`task_image_inputs`.
    """
    dockerfile = _validated_task_dockerfile(dockerfile_path, _DEFAULT_BUILD_TIMEOUT_SECONDS)
    return _task_image_tag(dockerfile, base_image)


def task_image_inputs(reference: str) -> str | None:
    """Return the build-input digest of a task image *reference*, or ``None`` for another image.

    Two references with the same digest were built from identical inputs,
    whichever directory the task was in.
    """
    if not is_task_image_reference(reference):
        return None
    _owner, separator, inputs = reference.removeprefix(f"{TASK_IMAGE_REPOSITORY}:").rpartition("-")
    return inputs if separator else None


def is_task_image_reference(image: str) -> bool:
    """Return whether *image* is a tag :func:`task_image_reference` produces."""
    return image.startswith(f"{TASK_IMAGE_REPOSITORY}:")


def local_image_id(
    image: str,
    *,
    command_runner: DockerBuildRunner,
    output: OutputSink,
    clock: Clock,
) -> str | None:
    """Return the immutable ID of *image* (a tag or an ID) in the local daemon, or ``None``.

    A failed ``docker image inspect`` means absent. Docker missing, the
    inspection outliving its bound, or an answer that is no image ID raises
    :class:`TaskImageBuildError`.
    """
    build = _BuildRun(
        runner=command_runner,
        timeout=_DEFAULT_VERIFY_TIMEOUT_SECONDS,
        output=output,
        clock=clock,
    )
    target = _BuildTarget(
        source=image,
        context=Path.cwd(),
        image_repository=TASK_IMAGE_REPOSITORY,
        image_label="task image",
    )
    argv = ("docker", "image", "inspect", "--format", "{{.Id}}", image)
    result = _run_docker(build, argv, action="inspecting", target=target)
    if result.returncode != 0:
        return None
    image_id = result.stdout.strip()
    if result.stdout_truncated or _IMAGE_ID.fullmatch(image_id) is None:
        displayed = result.stdout_diagnostic("<empty>")
        raise TaskImageBuildError.invalid_runnable_id(target.image_label, image, displayed)
    return image_id


def _task_image_tag(dockerfile: Path, base_image: str | None) -> str:
    """The task image's local tag: its owner, then its build inputs."""
    owner = _task_image_owner(dockerfile.parent)
    return f"{TASK_IMAGE_REPOSITORY}:{owner}-{_task_image_digest(dockerfile, base_image)}"


def _task_image_owner(context: Path) -> str:
    """A 16-character key of the task directory's absolute location."""
    return hashlib.sha256(str(context).encode()).hexdigest()[:16]


def _build_task_image(dockerfile: Path, *, base_image: str | None, build: _BuildRun) -> _BuiltImage:
    """Build a validated task Dockerfile to a content-addressed tag and return it with its ID."""
    context = dockerfile.parent
    extra_build_args = ("--build-arg", f"BASE_IMAGE={base_image}") if base_image is not None else ()
    target = _BuildTarget(
        source=str(dockerfile),
        context=context,
        image_repository=_TASK_IMAGE_REPOSITORY,
        image_label="task image",
    )
    image_tag = _task_image_tag(dockerfile, base_image)
    built = _build_and_inspect(target, extra_build_args, build, image_tag=image_tag)
    _prune_superseded_task_tags(build, context, keep_tag=image_tag)
    return built


def _task_image_digest(dockerfile: Path, base_image: str | None) -> str:
    """Hash a task's build inputs and *base_image* into a deterministic 32-character tag suffix.

    The task Dockerfile is always a direct child of its own build context
    (:func:`_validated_task_dockerfile` keeps the lexical parent as the
    context), so hashing every file the build sends to Docker (the context
    after ``.dockerignore``, plus the Dockerfile and ``.dockerignore``)
    covers every input Docker can read while building. Folding in
    *base_image* covers the one input outside the context. Paths are hashed
    relative to the context, so the same inputs hash the same wherever the
    task lives, mirroring how :func:`vs_sandbox.feature_image.build_feature_image`
    keys the agent layer's tag to its own inputs.
    """
    context = dockerfile.parent
    digest = hashlib.sha256()
    digest.update(repr(base_image).encode())
    digest.update(b"\0")
    files = {
        path.relative_to(context).as_posix(): path for path in context.rglob("*") if path.is_file()
    }
    dockerignore = files.get(DOCKERIGNORE)
    kept = build_context_inputs(
        files,
        dockerignore=(
            None
            if dockerignore is None
            else dockerignore.read_text(encoding="utf-8", errors="replace")
        ),
        dockerfile=dockerfile.relative_to(context).as_posix(),
    )
    for relative in kept:
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(files[relative].read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:32]


def _prune_superseded_task_tags(build: _BuildRun, context: Path, *, keep_tag: str) -> None:
    """Best-effort: remove this task's other local tags under :data:`TASK_IMAGE_REPOSITORY`.

    One launch resolves exactly one task image tag (*keep_tag*, keyed to this
    task's content); any other tag with the same owner prefix is left over
    from a since-changed version of this task and would otherwise accumulate
    forever, which is the bug this guards against. Tags of other tasks, other
    checkouts, other users of the daemon, and earlier VibeSys versions are
    never touched: a concurrent run may be using them. Removing a tag
    drops a name, not Docker's own layer cache, so a future build of a
    since-pruned task still reuses whatever layers it shares with others.
    Failing to list or remove a tag does not fail the build: the image this
    launch needed is already resolved.
    """
    timeout = min(_DEFAULT_VERIFY_TIMEOUT_SECONDS, build.remaining())
    if timeout <= 0:
        return
    try:
        listed = _bounded_result(
            build.runner.run(
                ("docker", "image", "ls", "--format", "{{.Tag}}", _TASK_IMAGE_REPOSITORY),
                cwd=context,
                timeout=timeout,
                output=build.output,
            )
        )
    except (FileNotFoundError, ProcessTimeoutError):
        return
    if listed.returncode != 0:
        return
    keep_suffix = keep_tag.removeprefix(f"{_TASK_IMAGE_REPOSITORY}:")
    owner = f"{_task_image_owner(context)}-"
    for tag in listed.stdout.split():
        if tag == keep_suffix or not tag.startswith(owner):
            continue
        timeout = min(_DEFAULT_VERIFY_TIMEOUT_SECONDS, build.remaining())
        if timeout <= 0:
            return
        with contextlib.suppress(FileNotFoundError, ProcessTimeoutError):
            build.runner.run(
                ("docker", "image", "rm", f"{_TASK_IMAGE_REPOSITORY}:{tag}"),
                cwd=context,
                timeout=timeout,
                output=build.output,
            )


def _resolvable_base(base_image: str, build: _BuildRun) -> str:
    """Return a reference to *base_image* that BuildKit resolves locally.

    A bare ``sha256:`` image ID (what the headless entrypoint records for a
    task image) is tagged ``vibesys-base:<id>`` first. Any other reference
    already names a tag or digest and is returned unchanged.
    """
    if _IMAGE_ID.fullmatch(base_image) is None:
        return base_image
    tag = f"{_BASE_IMAGE_REPOSITORY}:{base_image.removeprefix('sha256:')}"
    target = _BuildTarget(
        source=base_image,
        context=Path.cwd(),
        image_repository=_BASE_IMAGE_REPOSITORY,
        image_label="base image",
    )
    result = _run_docker(build, ("docker", "tag", base_image, tag), action="tagging", target=target)
    if result.returncode != 0:
        detail = result.failure_diagnostic("docker tag failed")
        raise TaskImageBuildError.base_tag_failed(base_image, tag, result.returncode, detail)
    return tag


def agent_image(  # noqa: PLR0913  # lint-waiver: LW-011126 [PLR0913]; Base/task image, Features, injected runner, timeout, progress sink, and output sink independently affect build layers, execution, or observation; callers use this public builder directly.
    base_image: str,
    *,
    task_dockerfile: Path | None = None,
    features: Sequence[FeatureRef] = (),
    command_runner: DockerBuildRunner | None = None,
    timeout: float = _DEFAULT_BUILD_TIMEOUT_SECONDS,
    on_progress: BuildProgressSink | None = None,
    output: OutputSink,
    clock: Clock,
) -> str:
    """Build the agent layer on top of a task image, and return its image ID.

    The agent layer is the ``vibesys-agent`` Feature (CLIs, the ``agent`` user,
    VibeSys's Python) followed by *features*, which the caller selects (a run
    environment's extras, toolchains from :mod:`vs_sandbox.toolchain_features`).
    :func:`vs_sandbox.feature_image.build_feature_image` applies them with one
    ``docker build`` of a generated Dockerfile; the result runs as the ``agent`` user in
    ``/workspace``.

    When ``task_dockerfile`` is given, the task image is built first (as
    :func:`build_task_image` does, with ``base_image`` passed through so the
    task Dockerfile may ``FROM ${BASE_IMAGE}``) and the agent layer is built on
    top of it by its unique local tag. Otherwise the agent layer is built
    directly on ``base_image``. BuildKit cannot resolve a bare ``sha256:`` ID
    as a base, so an ID ``base_image`` is given a local tag first.

    Agent versions come from :mod:`vs_agent.provider_policy`, so a bump changes
    no caller: rebuilding with an unchanged selection resolves to the same image
    from Docker's per-Feature layer cache.

    ``on_progress``, when given, receives each build's step observations
    (the task image's, then the agent layer's), parsed from Docker's
    progress output. ``output`` receives every chunk each Docker command of
    the build writes (the base tag, both builds, their inspections), in the
    order the commands run, as it is written.

    ``timeout`` is the budget for the whole build (the task image, the base
    tag, and the Feature build together). The Docker command that is running
    when it runs out is stopped, process group included, and
    :class:`ImageBuildTimeoutError` names the Dockerfile step and carries the
    tail of the build's output. ``clock`` measures the budget.
    """
    if timeout <= 0:
        message = "agent image build timeout must be positive"
        raise ValueError(message)

    runner = command_runner or SubprocessDockerBuildRunner()
    build = _BuildRun(
        runner=runner, timeout=timeout, on_progress=on_progress, output=output, clock=clock
    )
    if task_dockerfile is not None:
        dockerfile = _validated_task_dockerfile(task_dockerfile, timeout)
        base = _build_task_image(
            dockerfile,
            base_image=_resolvable_base(base_image, build),
            build=build,
        ).tag
    else:
        base = _resolvable_base(base_image, build)

    commands = _BuildCommandRunner(build)
    try:
        built = build_feature_image(
            base,
            (agent_feature(), *features),
            final=FinalImage(
                user=AGENT_USER, workdir=AGENT_WORKDIR, image_repository=AGENT_IMAGE_REPOSITORY
            ),
            command_runner=commands,
            timeout=timeout,
        )
    except FeatureImageBuildError as exc:
        if commands.expired is not None:
            raise commands.expired from exc
        raise TaskImageBuildError(str(exc)) from exc
    return built.image_id


@dataclass(frozen=True)
class _BuildRun:
    """How every build of one launch runs: its runner, budget, progress and output sinks.

    ``timeout`` is the budget for all of the launch's Docker commands together;
    each command gets what remains of it by ``clock``, so no sequence of
    commands can outlive the caller's limit. ``output`` receives the raw output
    of every Docker command the build runs.
    """

    runner: DockerBuildRunner
    timeout: float
    clock: Clock
    output: OutputSink
    on_progress: BuildProgressSink | None = None
    started: float = field(init=False)

    def __post_init__(self) -> None:
        """Start the budget now."""
        object.__setattr__(self, "started", self.clock.now())

    def remaining(self) -> float:
        """Seconds left of the budget; zero or less when it has run out."""
        return self.timeout - (self.clock.now() - self.started)


@dataclass(frozen=True)
class _FeatureCommandResult:
    """Bounded command output adapted to the Feature builder's narrower contract."""

    returncode: int
    stdout: str
    stderr: str


class _BuildCommandRunner:
    """Present a :class:`DockerBuildRunner` to the image mechanism.

    The mechanism reports timeouts as ``TimeoutError`` and knows nothing of
    progress or of the launch's shared budget, so this adapter clamps each
    command to what remains of the budget, translates an expired one while
    keeping what the build printed (``expired``), writes every command's
    output to the run's output sink, and, for each ``docker build`` it runs,
    reports progress to the run's progress sink.
    """

    def __init__(self, build: _BuildRun) -> None:
        self._build = build
        self.expired: ImageBuildTimeoutError | None = None

    def run(self, argv: Sequence[str], *, cwd: Path, timeout: float) -> _FeatureCommandResult:
        tracker = _StepTracker(self._build.on_progress if argv[1] == "build" else None)
        try:
            with Lines(tracker.line) as lines:
                result = _bounded_result(
                    self._build.runner.run(
                        argv,
                        cwd=cwd,
                        timeout=_command_budget(self._build, argv, timeout),
                        output=tee(self._build.output, lines),
                    )
                )
        except ProcessTimeoutError as exc:
            self.expired = _timeout_error(
                self._build, exc, action="building", subject="the agent image", step=tracker.running
            )
            raise TimeoutError(str(self.expired)) from exc
        return _FeatureCommandResult(
            returncode=result.returncode,
            stdout=(
                result.stdout_diagnostic("<empty>") if result.stdout_truncated else result.stdout
            ),
            stderr=(
                _diagnostic_text(
                    result.stderr,
                    result.stderr_omitted_characters,
                    fallback="<empty>",
                    limit=_DIAGNOSTIC_LIMIT,
                )
                if result.stderr_truncated
                else result.stderr
            ),
        )


def _command_budget(build: _BuildRun, argv: Sequence[str], limit: float) -> float:
    """Return the seconds one command may run: its own *limit*, within what the launch has left.

    Raises:
        ProcessTimeoutError: the launch's budget is already spent.
    """
    remaining = build.remaining()
    if remaining <= 0:
        raise ProcessTimeoutError(tuple(argv), build.timeout)
    return min(limit, remaining)


def _timeout_error(
    build: _BuildRun, exc: ProcessTimeoutError, *, action: str, subject: str, step: str
) -> ImageBuildTimeoutError:
    """Describe an expired command with the output tail the stopped process left."""
    stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
    stdout = exc.output.decode(errors="replace") if isinstance(exc.output, bytes) else exc.output
    tail = (stderr or "").strip() or (stdout or "").strip()
    return ImageBuildTimeoutError(
        build.timeout, action, subject, tail[-_TIMEOUT_OUTPUT_CHARACTERS:], step
    )


def _build_and_inspect(
    target: _BuildTarget, extra_build_args: Sequence[str], build: _BuildRun, *, image_tag: str
) -> _BuiltImage:
    """Build one Dockerfile to *image_tag* and resolve its immutable ID."""
    build_argv = (
        "docker",
        "build",
        "--provenance=false",
        "--tag",
        image_tag,
        "--file",
        target.source,
        *extra_build_args,
        str(target.context),
    )
    steps = _StepTracker(build.on_progress)
    build_result = _run_docker(
        build,
        build_argv,
        action="building",
        target=target,
        steps=steps,
    )
    if build_result.returncode != 0:
        detail = build_result.failure_diagnostic("docker build failed")
        raise TaskImageBuildError.build_failed(target, build_result.returncode, detail)

    inspect_argv = (
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        image_tag,
    )
    inspect_result = _run_docker(build, inspect_argv, action="inspecting", target=target)
    if inspect_result.returncode != 0:
        detail = inspect_result.failure_diagnostic("docker inspect failed")
        raise TaskImageBuildError.inspect_failed(
            target,
            image_tag,
            inspect_result.returncode,
            detail,
        )
    if inspect_result.stdout_truncated:
        displayed = inspect_result.stdout_diagnostic("<empty>")
        raise TaskImageBuildError.invalid_runnable_id(
            target.image_label,
            target.source,
            displayed,
        )
    image_id = inspect_result.stdout.strip()
    if _IMAGE_ID.fullmatch(image_id) is None:
        displayed = inspect_result.stdout_diagnostic("<empty>")
        raise TaskImageBuildError.invalid_runnable_id(
            target.image_label,
            target.source,
            displayed,
        )
    return _BuiltImage(tag=image_tag, image_id=image_id)


def _run_docker(
    build: _BuildRun,
    argv: Sequence[str],
    *,
    action: str,
    target: _BuildTarget,
    steps: _StepTracker | None = None,
) -> DockerCommandResult:
    tracker = steps or _StepTracker()
    try:
        with Lines(tracker.line) as lines:
            return _bounded_result(
                build.runner.run(
                    argv,
                    cwd=target.context,
                    timeout=_command_budget(build, argv, build.timeout),
                    output=tee(build.output, lines),
                )
            )
    except FileNotFoundError as exc:
        raise TaskImageBuildError.docker_not_found(action, target) from exc
    except ProcessTimeoutError as exc:
        raise _timeout_error(
            build,
            exc,
            action=action,
            subject=f"{target.image_label} from {target.source}",
            step=tracker.running,
        ) from exc


# ---------------------------------------------------------------------------
# Registry push and verification.
#
# A local Docker run never calls any of the functions below: it runs
# the image ``agent_image`` just built directly from the local Docker image
# store. Only a remote backend (Modal, SkyPilot) needs the image pulled from
# somewhere else, so only those callers push. A push is keyed by the local
# image ID rather than by tag: ``ensure_pushed`` checks whether *this exact*
# content was already pushed to *repository* before running `docker push`
# again, so a resumed or repeated remote launch against an unchanged agent
# image is a no-op past the first launch.
#
# Prerequisite, out of scope here: the caller's Docker daemon must already be
# logged in to the registry (``docker login ghcr.io`` with a token carrying
# `write:packages`; CI supplies this as `GITHUB_TOKEN`). Nothing in this
# module performs or inspects that login, and no function here logs
# credentials, only image references and exit codes.
# ---------------------------------------------------------------------------


def push_agent_image(
    image_id: str,
    *,
    repository: str = DEFAULT_AGENT_IMAGE_REGISTRY,
    command_runner: DockerBuildRunner | None = None,
    timeout: float = _DEFAULT_PUSH_TIMEOUT_SECONDS,
    output: OutputSink,
) -> str:
    """Tag, push, and resolve the pullable digest reference for *image_id*.

    *image_id* must be the immutable ``sha256:...`` ID :func:`agent_image` (or
    :func:`build_task_image`) returned, not a mutable tag. It is tagged as
    ``<repository>:<short id>`` (a name derived from the image's own content
    address, not a moving tag), pushed, and then resolved back to the
    ``<repository>@sha256:...`` manifest digest Docker recorded for that push,
    which is what a remote backend actually pulls.

    ``output`` receives every chunk the tag, push, and inspect commands write.

    Always pushes, even when the registry already has this content: callers
    that want to skip a redundant push when it is already verified present
    should call :func:`ensure_pushed` instead, which checks first.

    Raises:
        ValueError: if *image_id* is not an immutable image ID, or *timeout*
            is not positive.
        ImagePushError: if Docker is missing, the tag/push/inspect step times
            out or fails, or Docker reports no usable digest for the pushed
            tag. The docker CLI's own diagnostics are included; a failed push
            is very often an expired or missing registry login, so the
            message also names the `docker login` prerequisite.
    """
    if _IMAGE_ID.fullmatch(image_id) is None:
        message = f"push_agent_image requires an immutable image ID (sha256:...), got {image_id!r}"
        raise ValueError(message)
    if timeout <= 0:
        message = "agent image push timeout must be positive"
        raise ValueError(message)

    runner = command_runner or SubprocessDockerBuildRunner()
    registry = _RegistryCommands(runner, Path.cwd(), timeout, output)
    tag = f"{repository}:{image_id.removeprefix('sha256:')[:_SHORT_ID_LENGTH]}"

    tag_result = registry.run(("docker", "tag", image_id, tag), action="tagging")
    if tag_result.returncode != 0:
        detail = tag_result.failure_diagnostic("docker tag failed")
        raise ImagePushError.tag_failed(image_id, tag, tag_result.returncode, detail)

    push_result = registry.run(("docker", "push", tag), action="pushing")
    if push_result.returncode != 0:
        detail = push_result.failure_diagnostic("docker push failed")
        raise ImagePushError.push_failed(tag, push_result.returncode, detail)

    inspect_result = registry.run(
        ("docker", "inspect", "--format", "{{json .RepoDigests}}", tag), action="inspecting"
    )
    if inspect_result.returncode != 0 or not inspect_result.stdout.strip():
        detail = inspect_result.failure_diagnostic("no output")
        raise ImagePushError.digest_missing(tag, detail)
    if inspect_result.stdout_truncated:
        raise ImagePushError.repository_digest_missing(
            tag,
            repository,
            inspect_result.stdout_diagnostic("no output"),
        )
    # An image carries one repo digest per repository it has been pushed to
    # or pulled from, and the build tag's own digest can come first under the
    # containerd image store, so pick the entry for *repository* rather than
    # the first one.
    digest_reference = _repo_digest_for(inspect_result.stdout, repository)
    if digest_reference is None:
        raise ImagePushError.repository_digest_missing(
            tag,
            repository,
            inspect_result.stdout_diagnostic("no output"),
        )
    return digest_reference


def agent_image_is_pushed(
    reference: str,
    *,
    command_runner: DockerBuildRunner | None = None,
    timeout: float = _DEFAULT_VERIFY_TIMEOUT_SECONDS,
    output: OutputSink,
) -> bool:
    """Return whether *reference* (a ``repository@sha256:...`` digest) is live in its registry.

    Uses ``docker manifest inspect``, which asks the registry for the
    manifest without pulling any layers. A nonzero exit is treated uniformly
    as "not verified", whether the digest is genuinely absent, the registry
    is unreachable, or credentials are missing or expired: this function
    only answers "is it there right now", and :func:`push_agent_image`'s own
    error reporting is what surfaces which of those it was when a caller
    acts on a ``False`` result by trying to push. ``output`` receives what the
    inspection writes.
    """
    runner = command_runner or SubprocessDockerBuildRunner()
    try:
        result = runner.run(
            ("docker", "manifest", "inspect", reference),
            cwd=Path.cwd(),
            timeout=timeout,
            output=output,
        )
    except (FileNotFoundError, ProcessTimeoutError):
        return False
    return result.returncode == 0


def ensure_pushed(
    image_id: str,
    *,
    repository: str = DEFAULT_AGENT_IMAGE_REGISTRY,
    command_runner: DockerBuildRunner | None = None,
    timeout: float = _DEFAULT_PUSH_TIMEOUT_SECONDS,
    output: OutputSink,
) -> str:
    """Return a pullable ``repository@sha256:...`` reference for *image_id*, pushing only if needed.

    Checks Docker's own record of where this exact local image was already
    pushed (``docker image inspect``'s ``RepoDigests``, which persists across
    processes as long as the local image store keeps the image) and, when a
    digest for *repository* is on file, confirms it is still live in the
    registry via :func:`agent_image_is_pushed` before trusting it. Only when
    neither check succeeds does this actually push, via
    :func:`push_agent_image`, and it re-verifies the result before returning
    so a caller never receives a digest this function has not confirmed is
    fetchable. ``output`` receives every chunk each Docker command writes.

    Raises:
        ImagePushError: if pushing fails, or if the registry does not report
            the freshly pushed digest as present. The message names the
            digest that could not be verified.
    """
    runner = command_runner or SubprocessDockerBuildRunner()
    cached = _cached_repo_digest(
        image_id, repository, runner=runner, timeout=timeout, output=output
    )
    if cached is not None and agent_image_is_pushed(
        cached, command_runner=runner, timeout=timeout, output=output
    ):
        return cached

    pushed = push_agent_image(
        image_id, repository=repository, command_runner=runner, timeout=timeout, output=output
    )
    if not agent_image_is_pushed(pushed, command_runner=runner, timeout=timeout, output=output):
        raise ImagePushError.digest_not_visible(pushed)
    return pushed


def _cached_repo_digest(
    image_id: str,
    repository: str,
    *,
    runner: DockerBuildRunner,
    timeout: float,
    output: OutputSink,
) -> str | None:
    """Return a previously pushed ``repository@sha256:...`` digest for *image_id*, if known.

    Reads *image_id*'s ``RepoDigests`` from the local Docker image store,
    which Docker updates in place on every successful ``docker push`` of a
    tag pointing at that image ID. Absent, malformed, or Docker-unreachable
    output all resolve to "unknown" rather than raising: the caller falls
    back to actually pushing, which reports the real failure if one exists.
    """
    try:
        result = _bounded_result(
            runner.run(
                ("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image_id),
                cwd=Path.cwd(),
                timeout=timeout,
                output=output,
            )
        )
    except (FileNotFoundError, ProcessTimeoutError):
        return None
    if result.returncode != 0 or result.stdout_truncated:
        return None
    return _repo_digest_for(result.stdout, repository)


_ANY_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_MAX_INSPECT_BYTES = 1024 * 1024
"""The largest ``RepoDigests`` listing read from ``docker image inspect``."""


def _repo_digest_for(repo_digests_json: str, repository: str) -> str | None:
    """Pick the ``repository@sha256:...`` entry out of a ``RepoDigests`` JSON list.

    Absent, malformed, or non-list output resolves to ``None``.
    """
    digests = decode_json(
        repo_digests_json.strip() or "null",
        _ANY_JSON,
        source="docker image inspect RepoDigests",
        max_bytes=_MAX_INSPECT_BYTES,
    )
    if not isinstance(digests, list):
        return None
    prefix = f"{repository}@sha256:"
    return next(
        (entry for entry in digests if isinstance(entry, str) and entry.startswith(prefix)),
        None,
    )


@dataclass(frozen=True)
class _RegistryCommands:
    """How one push runs its Docker commands: runner, directory, timeout, and output sink."""

    runner: DockerBuildRunner
    cwd: Path
    timeout: float
    output: OutputSink

    def run(self, argv: Sequence[str], *, action: str) -> DockerCommandResult:
        """Run one registry command, translating a missing Docker and a timeout."""
        try:
            return _bounded_result(
                self.runner.run(argv, cwd=self.cwd, timeout=self.timeout, output=self.output)
            )
        except FileNotFoundError as exc:
            raise ImagePushError.docker_not_found(action) from exc
        except ProcessTimeoutError as exc:
            raise ImagePushError.docker_timed_out(self.timeout, action) from exc
