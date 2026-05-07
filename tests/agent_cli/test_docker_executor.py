from __future__ import annotations

import io

from agentshim.executor import CallbackCommandStreamSink, CommandRequest
from vibeserve_agent.agents.docker_executor import DockerCommandExecutor, DockerCommandHandle


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


def test_docker_executor_runs_command_request_and_streams_to_sink(monkeypatch):
    process = _FakeProcess()
    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        return process

    monkeypatch.setattr("vibeserve_agent.agents.docker_executor.subprocess.Popen", fake_popen)
    stdout: list[str] = []
    stderr: list[str] = []
    started: list[DockerCommandHandle] = []

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
    assert started[0].process is process
    assert result.returncode == 0
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
