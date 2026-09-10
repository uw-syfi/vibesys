from __future__ import annotations

import json
import subprocess
import threading
from typing import TYPE_CHECKING

import pytest
from agentshim import CallbackCommandStreamSink, CommandRequest, CommandResult
from agentshim.testing import FakeExecutor, FakeRun

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

    def test_replays_a_stable_completed_rollout_and_reports_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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

        stdout: list[str] = []
        result = executor.run(
            _request(argv=["codex", "exec", "resume", THREAD_ID, "-", "--json"], timeout=7200.0),
            _sink(stdout),
        )

        assert [json.loads(line) for line in stdout] == [
            {
                "type": "item.completed",
                "item": {
                    "id": "vibesys-codex-rollout-watchdog",
                    "type": "agent_message",
                    "text": completion.message,
                },
            },
            {"type": "turn.completed"},
        ]
        assert result.returncode == 0
        assert result.stdout == "".join(stdout)
        assert any(
            cmd[:4] == ["docker", "exec", "container-123", "python3"] and THREAD_ID in cmd
            for cmd in docker_calls
        )
        assert len(logs) == 1
        assert "rollout file" in logs[0]

    def test_learns_the_thread_id_from_a_fresh_run_stream(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        completion = _CodexRolloutCompletion(fingerprint="ts", message="done")
        inner = _StalledExecutor(
            stdout=[json.dumps({"type": "thread.started", "thread_id": THREAD_ID}) + "\n"]
        )
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
