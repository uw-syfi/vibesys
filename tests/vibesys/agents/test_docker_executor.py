from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import TYPE_CHECKING

import pytest
from agentshim import (
    CallbackCommandStreamSink,
    CliAgent,
    CommandRequest,
    CommandResult,
    TurnRequest,
)
from agentshim.testing import FakeExecutor, FakeRun, scripted_turn

from vibesys.agents.docker_executor import (
    CodexRolloutWatchdogExecutor,
    DockerCommandExecutor,
    _codex_resume_thread_id,
    _codex_started_thread_id,
    _CodexRolloutCompletion,
    repair_workspace_ownership,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentshim import CommandStreamSink

THREAD_ID = "019fc654-87f2-7702-8bf2-05b6f4f006dc"


def _sink(
    stdout: list[str] | None = None,
    stderr: list[str] | None = None,
) -> CallbackCommandStreamSink:
    return CallbackCommandStreamSink(
        on_stdout=(stdout if stdout is not None else []).append,
        on_stderr=(stderr if stderr is not None else []).append,
    )


def _request(**overrides: object) -> CommandRequest:
    fields: dict[str, object] = {
        "argv": ["claude", "-p"],
        "stdin": "prompt",
        "cwd": None,
        "env": {},
        "timeout": 17.0,
    }
    fields.update(overrides)
    return CommandRequest(**fields)  # type: ignore[arg-type]


class TestDockerCommandExecutor:
    """The docker exec transport: argv shape, environment, and binary lookup."""

    def test_wraps_argv_in_docker_exec_with_the_request_working_directory(self) -> None:
        inner = FakeExecutor(FakeRun(stdout=["out\n"], stderr=["err\n"]))
        executor = DockerCommandExecutor(lambda: "container-123", inner=inner)
        stdout: list[str] = []
        stderr: list[str] = []

        result = executor.run(
            _request(argv=["codex", "exec", "-"], cwd="/workspace/candidate"),
            _sink(stdout, stderr),
        )

        assert inner.requests[0].argv == [
            "docker",
            "exec",
            "-i",
            "-w",
            "/workspace/candidate",
            "container-123",
            "codex",
            "exec",
            "-",
        ]
        assert inner.requests[0].cwd is None
        assert inner.requests[0].timeout == 17.0
        assert stdout == ["out\n"]
        assert stderr == ["err\n"]
        assert result.returncode == 0

    def test_falls_back_to_the_configured_workdir_when_the_request_has_no_cwd(self) -> None:
        inner = FakeExecutor(FakeRun())
        DockerCommandExecutor(lambda: "container-123", inner=inner).run(_request(), _sink())

        assert inner.requests[0].argv[:6] == [
            "docker",
            "exec",
            "-i",
            "-w",
            "/workspace",
            "container-123",
        ]

    def test_forwards_only_the_nominated_environment_entries(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The host environment describes the host, not the container.

        ``PATH`` and the rest of the turn environment name directories that do
        not exist inside the image, so only the variables the caller nominated
        cross the boundary.
        """
        monkeypatch.setenv("PATH", "/host/bin")
        inner = FakeExecutor(FakeRun())

        DockerCommandExecutor(
            lambda: "container-123",
            forward_env=("ANTHROPIC_AUTH_TOKEN", "VIBESYS_ROUND", "ABSENT"),
            inner=inner,
        ).run(
            _request(
                env={
                    "PATH": "/host/bin",
                    "SHARED": "same",
                    "ANTHROPIC_AUTH_TOKEN": "token",
                    "VIBESYS_ROUND": "3",
                }
            ),
            _sink(),
        )

        argv = inner.requests[0].argv
        forwarded = [argv[index + 1] for index, part in enumerate(argv) if part == "-e"]
        # A nominated variable the turn does not carry is simply absent; it is
        # not forwarded as an empty value that would shadow the image's own.
        assert forwarded == ["ANTHROPIC_AUTH_TOKEN=token", "VIBESYS_ROUND=3"]
        # The docker CLI itself still runs with the host environment.
        assert inner.requests[0].env["PATH"] == "/host/bin"

    def test_forwards_nothing_when_the_caller_nominated_nothing(self) -> None:
        inner = FakeExecutor(FakeRun())

        DockerCommandExecutor(lambda: "container-123", inner=inner).run(
            _request(env={"PATH": "/host/bin", "VIBESYS_ROUND": "3"}),
            _sink(),
        )

        assert "-e" not in inner.requests[0].argv

    def test_passes_stdin_through_untouched(self) -> None:
        inner = FakeExecutor(FakeRun())
        DockerCommandExecutor(lambda: "container-123", inner=inner).run(
            _request(stdin="a long prompt"),
            _sink(),
        )

        assert inner.requests[0].stdin == "a long prompt"

    def test_reads_the_container_id_once_per_request(self) -> None:
        containers = iter(["container-a", "container-b"])
        inner = FakeExecutor(FakeRun())
        executor = DockerCommandExecutor(lambda: next(containers), inner=inner)

        executor.run(_request(), _sink())
        executor.run(_request(), _sink())

        assert [request.argv[5] for request in inner.requests] == [
            "container-a",
            "container-b",
        ]

    def test_finds_the_binary_by_name_because_it_lives_in_the_container(self) -> None:
        executor = DockerCommandExecutor(lambda: "container-123", inner=FakeExecutor(FakeRun()))

        assert executor.find_binary("codex", {"PATH": "/host/bin"}) == "codex"

    def test_checks_the_binary_inside_the_container(self) -> None:
        inner = FakeExecutor(FakeRun())
        executor = DockerCommandExecutor(lambda: "container-123", inner=inner)

        executor.check_binary("claude", {}, timeout=5)

        assert inner.requests[0].argv == [
            "docker",
            "exec",
            "-i",
            "-w",
            "/workspace",
            "container-123",
            "claude",
            "--help",
        ]


class TestRepairWorkspaceOwnership:
    """Root-owned replacements in the bind mount must go back to the host user."""

    def test_runs_the_chown_sweep_inside_the_container(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("vibesys.agents.docker_executor.subprocess.run", fake_run)

        repair_workspace_ownership("container-123", uid=123, gid=456)

        assert calls == [
            (
                [
                    "docker",
                    "exec",
                    "container-123",
                    "find",
                    "/workspace",
                    "-xdev",
                    "-user",
                    "0",
                    "-writable",
                    "-exec",
                    "chown",
                    "123:456",
                    "{}",
                    "+",
                ],
                {
                    "capture_output": True,
                    "text": True,
                    "timeout": 120,
                    "check": False,
                },
            )
        ]

    def test_reports_the_command_diagnostic_when_the_sweep_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "vibesys.agents.docker_executor.subprocess.run",
            lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 1, "", "chown: no such user"),
        )

        with pytest.raises(RuntimeError) as excinfo:
            repair_workspace_ownership("container-123", uid=1, gid=2)

        assert str(excinfo.value) == (
            "failed to restore writable Docker workspace ownership: chown: no such user"
        )


class _BlockingHandle:
    """Handle whose stop calls release the inner run, like a real process."""

    def __init__(self, released: threading.Event) -> None:
        self._released = released
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self._released.set()

    def kill(self) -> None:
        self.killed = True
        self._released.set()


class _StalledExecutor:
    """Inner executor that streams a line and then hangs until it is stopped."""

    def __init__(self, stdout: list[str], returncode: int = -9) -> None:
        self._stdout = stdout
        self._returncode = returncode
        self.released = threading.Event()
        self.handle: _BlockingHandle | None = None

    def find_binary(self, name: str, env: Mapping[str, str]) -> str:
        del env
        return name

    def check_binary(self, path: str, env: Mapping[str, str], *, timeout: float) -> None:
        del path, env, timeout

    def run(self, request: CommandRequest, sink: CommandStreamSink) -> CommandResult:
        del request
        self.handle = _BlockingHandle(self.released)
        sink.started(self.handle)
        for line in self._stdout:
            sink.stdout(line)
        assert self.released.wait(timeout=10), "watchdog never stopped the stalled run"
        return CommandResult(
            returncode=self._returncode,
            stdout="".join(self._stdout),
            stderr="",
        )


def _impatient(executor: CodexRolloutWatchdogExecutor) -> CodexRolloutWatchdogExecutor:
    """Collapse the watchdog's real-time budgets so a test can drive it."""
    executor.tick_seconds = 0.001
    executor.rollout_poll_seconds = 0.0
    executor.completion_grace_seconds = 0.0
    executor.termination_grace_seconds = 1.0
    return executor


class TestCodexRolloutWatchdog:
    """Compensation for a resumed Codex run that completes but never exits."""

    def test_passes_a_non_codex_command_straight_through(self) -> None:
        inner = FakeExecutor(FakeRun(stdout=["hello\n"], returncode=3))
        stdout: list[str] = []

        result = CodexRolloutWatchdogExecutor(inner, lambda: "container-123").run(
            _request(argv=["claude", "-p", "--verbose"]),
            _sink(stdout),
        )

        assert stdout == ["hello\n"]
        assert result.returncode == 3

    def test_leaves_the_exit_code_alone_when_it_never_intervenes(self) -> None:
        inner = FakeExecutor(FakeRun(stdout=["{}\n"], returncode=7))

        result = CodexRolloutWatchdogExecutor(inner, lambda: "container-123").run(
            _request(argv=["codex", "exec", "resume", THREAD_ID, "-", "--json"]),
            _sink(),
        )

        assert result.returncode == 7

    def test_delegates_binary_lookup_and_the_health_check(self) -> None:
        inner = FakeExecutor(FakeRun())
        executor = CodexRolloutWatchdogExecutor(inner, lambda: "container-123")

        assert executor.find_binary("codex", {}) == "/usr/local/bin/codex"
        executor.check_binary("/usr/local/bin/codex", {}, timeout=5)
        assert inner.checked == ["/usr/local/bin/codex"]

    def test_replays_a_stable_completed_rollout_as_a_finished_turn(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The replay has to read back as a real Codex turn, not as JSON that looks right.

        The synthesized frames exist to reach agentshim's own Codex parser, so
        they are asserted through it: a session built on the watchdog reports
        the recovered message as the turn's answer.
        """
        completion = _CodexRolloutCompletion(
            fingerprint="2026-08-03T10:21:04.655Z",
            message='{"hypothesis_outcome":"inconclusive"}',
        )
        inner = _StalledExecutor(stdout=[])
        logs: list[str] = []
        executor = _impatient(
            CodexRolloutWatchdogExecutor(inner, lambda: "container-123", log=logs.append)
        )
        docker_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            docker_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "24190\n", "")

        monkeypatch.setattr("vibesys.agents.docker_executor.subprocess.run", fake_run)
        monkeypatch.setattr(
            executor,
            "_read_codex_rollout_completion",
            lambda _container, _thread: completion,
        )
        agent = CliAgent("codex", executor=executor)
        # A resumed turn is the case the watchdog exists for.
        session = agent.start_session(session_id=THREAD_ID)

        result = session.turn(TurnRequest(prompt="do it", timeout=7200.0))

        assert result.text == completion.message
        assert result.exit_code == 0
        assert any(
            cmd[:4] == ["docker", "exec", "container-123", "python3"] for cmd in docker_calls
        )
        assert len(logs) == 1
        assert "rollout file" in logs[0]

    def test_learns_the_thread_id_from_a_fresh_run_stream(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        completion = _CodexRolloutCompletion(fingerprint="ts", message="done")
        # The provider's real stream format, from the library that owns it.
        inner = _StalledExecutor(stdout=list(scripted_turn("codex", session_id=THREAD_ID).stdout))
        executor = _impatient(CodexRolloutWatchdogExecutor(inner, lambda: "container-123"))
        seen_threads: list[str] = []

        monkeypatch.setattr(
            "vibesys.agents.docker_executor.subprocess.run",
            lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 0, "24190\n", ""),
        )

        def read(_container: str, thread: str) -> _CodexRolloutCompletion:
            seen_threads.append(thread)
            return completion

        monkeypatch.setattr(executor, "_read_codex_rollout_completion", read)

        result = executor.run(_request(argv=["codex", "exec", "-", "--json"]), _sink())

        assert set(seen_threads) == {THREAD_ID}
        assert result.returncode == 0

    def test_stops_a_docker_exec_that_outlived_its_codex_process(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The watchdog killed the run, so the signal it caused is not the answer."""
        inner = _StalledExecutor(stdout=[])
        logs: list[str] = []
        executor = _impatient(
            CodexRolloutWatchdogExecutor(inner, lambda: "container-123", log=logs.append)
        )

        monkeypatch.setattr(
            "vibesys.agents.docker_executor.subprocess.run",
            lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 1, "", ""),
        )
        monkeypatch.setattr(
            executor,
            "_read_codex_rollout_completion",
            lambda _container, _thread: None,
        )

        result = executor.run(
            _request(argv=["codex", "exec", "resume", THREAD_ID, "-", "--json"]),
            _sink(),
        )

        assert inner.handle is not None
        assert inner.handle.killed
        assert result.returncode == 0
        assert len(logs) == 1
        assert "never exited" in logs[0]

    def test_a_run_that_ended_on_its_own_keeps_its_exit_code(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``pgrep`` finding no codex is not evidence the turn succeeded.

        A Codex that crashed leaves ``pgrep`` nothing to find either, and its
        ``docker exec`` then ends on its own carrying the failure's exit code.
        The watchdog clears an exit code only when it caused one, so a crashed
        turn is not reported as a successful one.
        """
        inner = _StalledExecutor(stdout=[], returncode=2)
        logs: list[str] = []
        executor = _impatient(
            CodexRolloutWatchdogExecutor(inner, lambda: "container-123", log=logs.append)
        )
        monkeypatch.setattr(
            executor,
            "_read_codex_rollout_completion",
            lambda _container, _thread: None,
        )

        def crashed(_container: str, _binary: str) -> bool:
            # The CLI died and its `docker exec` is already unwinding: release
            # the inner run, then answer the liveness probe. By the time the
            # watchdog acts on the answer the run has returned on its own.
            inner.released.set()
            time.sleep(0.05)
            return False

        monkeypatch.setattr(executor, "_codex_process_alive", crashed)

        result = executor.run(
            _request(argv=["codex", "exec", "resume", THREAD_ID, "-", "--json"]),
            _sink(),
        )

        assert result.returncode == 2
        assert inner.handle is not None
        assert not inner.handle.killed
        assert logs == []

    def test_a_recovered_completion_clears_the_exit_code_the_kill_caused(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The rollout proves the turn finished, whatever signal stopped the shell."""
        completion = _CodexRolloutCompletion(fingerprint="ts", message="done")
        inner = _StalledExecutor(stdout=[], returncode=-15)
        executor = _impatient(CodexRolloutWatchdogExecutor(inner, lambda: "container-123"))

        monkeypatch.setattr(
            "vibesys.agents.docker_executor.subprocess.run",
            lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 0, "24190\n", ""),
        )
        monkeypatch.setattr(
            executor,
            "_read_codex_rollout_completion",
            lambda _container, _thread: completion,
        )

        result = executor.run(
            _request(argv=["codex", "exec", "resume", THREAD_ID, "-", "--json"]),
            _sink(),
        )

        assert result.returncode == 0
        assert "done" in result.stdout


def _rollout_line(payload_type: str, timestamp: str, **payload: object) -> str:
    """One ``event_msg`` line the way a Codex rollout file writes it."""
    return json.dumps(
        {
            "type": "event_msg",
            "timestamp": timestamp,
            "payload": {"type": payload_type, **payload},
        }
    )


class _FakeDockerQueries:
    """Answer the watchdog's ``docker exec find``/``tail`` probes from memory."""

    def __init__(
        self,
        *,
        rollout: list[str] | None,
        path: str = "/root/.codex/sessions/r.jsonl",
    ) -> None:
        """Serve *rollout* as the file at *path*, or no file at all when None."""
        self.rollout = rollout
        self.path = path
        self.commands: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(cmd)
        if "find" in cmd:
            if self.rollout is None:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, f"{self.path}\n", "")
        if "tail" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "\n".join(self.rollout or []), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


class TestCodexRolloutReading:
    """What counts as stable terminal evidence in a resumed Codex rollout."""

    def _read(
        self,
        monkeypatch: pytest.MonkeyPatch,
        queries: _FakeDockerQueries,
    ) -> _CodexRolloutCompletion | None:
        executor = CodexRolloutWatchdogExecutor(FakeExecutor(FakeRun()), lambda: "container-123")
        monkeypatch.setattr("vibesys.agents.docker_executor.subprocess.run", queries)
        return executor._read_codex_rollout_completion("container-123", THREAD_ID)  # noqa: SLF001

    def test_no_rollout_file_is_no_evidence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        queries = _FakeDockerQueries(rollout=None)

        assert self._read(monkeypatch, queries) is None
        assert not any("tail" in cmd for cmd in queries.commands)

    def test_a_rollout_still_working_is_no_evidence(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The last lifecycle event is ``task_started``: the turn is still running."""
        queries = _FakeDockerQueries(
            rollout=[
                _rollout_line("task_started", "2026-08-03T10:00:00.000Z"),
                _rollout_line("agent_message", "2026-08-03T10:00:01.000Z", message="earlier"),
                _rollout_line("task_complete", "2026-08-03T10:00:02.000Z"),
                _rollout_line("task_started", "2026-08-03T10:00:03.000Z"),
            ]
        )

        assert self._read(monkeypatch, queries) is None

    def test_a_completed_rollout_reports_its_last_message(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        queries = _FakeDockerQueries(
            rollout=[
                _rollout_line("task_started", "2026-08-03T10:00:00.000Z"),
                _rollout_line("agent_message", "2026-08-03T10:00:01.000Z", message="first"),
                _rollout_line("agent_message", "2026-08-03T10:00:02.000Z", message="final"),
                _rollout_line("task_complete", "2026-08-03T10:00:03.000Z"),
                # Written after the turn finished, so it is not this turn's answer.
                _rollout_line("agent_message", "2026-08-03T10:00:04.000Z", message="later"),
            ]
        )

        completion = self._read(monkeypatch, queries)

        assert completion == _CodexRolloutCompletion(
            fingerprint="2026-08-03T10:00:03.000Z",
            message="final",
        )

    def test_a_completion_with_no_message_is_no_evidence(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        queries = _FakeDockerQueries(
            rollout=[
                _rollout_line("task_started", "2026-08-03T10:00:00.000Z"),
                _rollout_line("task_complete", "2026-08-03T10:00:03.000Z"),
            ]
        )

        assert self._read(monkeypatch, queries) is None

    def test_a_new_completion_changes_the_fingerprint(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The fingerprint is what makes the watchdog wait for a stable reading.

        A second turn completing in the same rollout must not be mistaken for
        the same terminal state held across two polls.
        """
        first = _FakeDockerQueries(
            rollout=[
                _rollout_line("task_started", "2026-08-03T10:00:00.000Z"),
                _rollout_line("agent_message", "2026-08-03T10:00:01.000Z", message="one"),
                _rollout_line("task_complete", "2026-08-03T10:00:02.000Z"),
            ]
        )
        second = _FakeDockerQueries(
            rollout=[
                *(first.rollout or []),
                _rollout_line("task_started", "2026-08-03T10:00:03.000Z"),
                _rollout_line("agent_message", "2026-08-03T10:00:04.000Z", message="two"),
                _rollout_line("task_complete", "2026-08-03T10:00:05.000Z"),
            ]
        )

        before = self._read(monkeypatch, first)
        after = self._read(monkeypatch, second)

        assert before is not None
        assert after is not None
        assert before.fingerprint != after.fingerprint
        assert after.message == "two"


class TestCodexArgvRecognition:
    """Only a machine-readable Codex exec run is watched."""

    def test_resume_thread_id_requires_a_json_codex_exec_run(self) -> None:
        assert (
            _codex_resume_thread_id(["codex", "exec", "resume", THREAD_ID, "-", "--json"])
            == THREAD_ID
        )
        assert _codex_resume_thread_id(["codex", "exec", "--json"]) is None
        assert _codex_resume_thread_id(["claude", "exec", "resume", THREAD_ID, "--json"]) is None

    def test_started_thread_id_rejects_a_value_that_is_not_an_identifier(self) -> None:
        stdout_lines = [
            "not-json\n",
            json.dumps({"type": "thread.started", "thread_id": THREAD_ID}) + "\n",
            json.dumps({"type": "turn.started"}) + "\n",
        ]

        assert _codex_started_thread_id(stdout_lines) == THREAD_ID
        assert (
            _codex_started_thread_id(
                [json.dumps({"type": "thread.started", "thread_id": "../../unsafe"})]
            )
            is None
        )
