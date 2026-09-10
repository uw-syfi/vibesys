from __future__ import annotations

import io
import json
import subprocess

from agentshim.executor import CallbackCommandStreamSink, CommandHandle, CommandRequest

from vibesys.agents.docker_executor import (
    DockerCommandExecutor,
    DockerCommandHandle,
    _CodexRolloutCompletion,
)


class _WritableStdin:
    def __init__(self) -> None:
        self.value = ""
        self.closed = False

    def write(self, value: str) -> None:
        self.value += value

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    pid = 12345

    def __init__(self) -> None:
        self.stdin = _WritableStdin()
        self.stdout = io.StringIO("out\n")
        self.stderr = io.StringIO("err\n")
        self.returncode: int | None = None
        self.wait_timeout: int | None = None

    def wait(self, timeout: int | None = None) -> int:
        self.wait_timeout = timeout
        self.returncode = 0
        return 0

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9

    def terminate(self) -> None:
        self.returncode = -15


class _HungProcess(_FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")

    def wait(self, timeout: int | None = None) -> int:
        self.wait_timeout = timeout
        if self.returncode is None:
            # A still-running fake is only ever waited on with a bounded
            # timeout; an unbounded wait here would hang the real executor.
            assert timeout is not None
            raise subprocess.TimeoutExpired(["docker", "exec"], timeout)
        return self.returncode


def test_docker_executor_runs_command_request_and_streams_to_sink(monkeypatch):  # noqa: ANN001, ANN202  # tracked: #288
    process = _FakeProcess()
    popen_calls = []

    def fake_popen(cmd, **kwargs):  # noqa: ANN001, ANN003, ANN202  # tracked: #288
        popen_calls.append((cmd, kwargs))
        return process

    monkeypatch.setattr("vibesys.agents.docker_executor.subprocess.Popen", fake_popen)
    stdout: list[str] = []
    stderr: list[str] = []
    started: list[CommandHandle] = []

    result = DockerCommandExecutor("container-123").run(
        CommandRequest(
            argv=["codex", "exec", "-"],
            stdin="prompt",
            cwd="/ignored",
            env={"PATH": "/ignored"},
            timeout=17,
        ),
        CallbackCommandStreamSink(
            on_stdout=stdout.append,
            on_stderr=stderr.append,
            on_started=started.append,
        ),
    )

    assert popen_calls[0][0] == [
        "docker",
        "exec",
        "-i",
        "-w",
        "/workspace",
        "container-123",
        "codex",
        "exec",
        "-",
    ]
    assert process.stdin.value == "prompt"
    assert process.stdin.closed
    assert process.wait_timeout == 17
    assert stdout == ["out\n"]
    assert stderr == ["err\n"]
    assert isinstance(started[0], DockerCommandHandle)
    assert started[0].process is process
    assert result.returncode == 0
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"


def test_docker_executor_repairs_workspace_ownership(monkeypatch):  # noqa: ANN001, ANN202  # tracked: #288
    calls = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003, ANN202  # tracked: #288
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("vibesys.agents.docker_executor.subprocess.run", fake_run)

    DockerCommandExecutor("container-123").repair_workspace_ownership(uid=123, gid=456)

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


def test_docker_executor_recovers_stable_completed_codex_rollout(monkeypatch):  # noqa: ANN001, ANN202  # tracked: #288
    process = _HungProcess()
    thread_id = "019fc654-87f2-7702-8bf2-05b6f4f006dc"
    completion = _CodexRolloutCompletion(
        fingerprint="2026-08-03T10:21:04.655Z",
        message='{"hypothesis_outcome":"inconclusive"}',
    )
    executor = DockerCommandExecutor("container-123")
    executor._CODEX_ROLLOUT_POLL_SECONDS = 0  # noqa: SLF001  # tracked: #288
    executor._CODEX_COMPLETION_GRACE_SECONDS = 0  # noqa: SLF001  # tracked: #288

    monkeypatch.setattr(
        "vibesys.agents.docker_executor.subprocess.Popen",
        lambda *args, **kwargs: process,  # noqa: ARG005  # tracked: #288
    )
    monkeypatch.setattr(
        "vibesys.agents.docker_executor.subprocess.run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, "24190\n", ""),  # noqa: ARG005  # tracked: #288
    )
    monkeypatch.setattr(executor, "_read_codex_rollout_completion", lambda _: completion)

    terminated: list[str] = []

    def terminate(completed_thread_id: str) -> None:
        terminated.append(completed_thread_id)
        process.kill()

    monkeypatch.setattr(executor, "_terminate_codex_resume", terminate)

    stdout: list[str] = []
    result = executor.run(
        CommandRequest(
            argv=["codex", "exec", "resume", thread_id, "-", "--json"],
            stdin="prompt",
            cwd="/ignored",
            env={},
            timeout=7200,
        ),
        CallbackCommandStreamSink(
            on_stdout=stdout.append,
            on_stderr=lambda _: None,
            on_started=lambda _: None,
        ),
    )

    events = [json.loads(line) for line in stdout]
    assert terminated == [thread_id]
    assert result.returncode == 0
    assert events == [
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


def test_codex_rollout_watchdog_requires_resumed_json_thread():  # noqa: ANN202  # tracked: #288
    thread_id = "019fc654-87f2-7702-8bf2-05b6f4f006dc"

    assert (
        DockerCommandExecutor._codex_resume_thread_id(  # noqa: SLF001  # tracked: #288
            ["codex", "exec", "resume", thread_id, "-", "--json"]
        )
        == thread_id
    )
    assert DockerCommandExecutor._codex_resume_thread_id(["codex", "exec", "--json"]) is None  # noqa: SLF001  # tracked: #288
    assert (
        DockerCommandExecutor._codex_resume_thread_id(  # noqa: SLF001  # tracked: #288
            ["claude", "exec", "resume", thread_id, "--json"]
        )
        is None
    )


def test_codex_rollout_watchdog_learns_fresh_thread_from_stream():  # noqa: ANN202  # tracked: #288
    thread_id = "019fc654-87f2-7702-8bf2-05b6f4f006dc"
    stdout_lines = [
        "not-json\n",
        json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n",
        json.dumps({"type": "turn.started"}) + "\n",
    ]

    assert DockerCommandExecutor._codex_started_thread_id(stdout_lines) == thread_id  # noqa: SLF001  # tracked: #288
    assert (
        DockerCommandExecutor._codex_started_thread_id(  # noqa: SLF001  # tracked: #288
            [json.dumps({"type": "thread.started", "thread_id": "../../unsafe"})]
        )
        is None
    )
