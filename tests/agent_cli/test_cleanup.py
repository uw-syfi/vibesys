"""CLI wrapper tests at the command-executor boundary."""

import subprocess

import pytest

from agentshim.executor import CommandRequest, CommandResult, CommandStreamSink
from libs.agent_cli.claude import ClaudeCodeCodingAgent
from libs.agent_cli.cli_agent import CLICodingAgent
from libs.agent_cli.codex import CodexCodingAgent
from libs.agent_cli.gemini import GeminiCodingAgent


class RecordingExecutor:
    def __init__(
        self,
        *,
        stdout: list[str] | None = None,
        stderr: list[str] | None = None,
        returncode: int = 0,
        run_error: BaseException | None = None,
    ) -> None:
        self.stdout = stdout or ["executor output\n"]
        self.stderr = stderr or []
        self.returncode = returncode
        self.run_error = run_error
        self.find_calls: list[tuple[str, dict[str, str]]] = []
        self.check_calls: list[tuple[str, dict[str, str], int]] = []
        self.run_calls: list[CommandRequest] = []

    def find_binary(self, binary_name: str, env: dict[str, str]) -> str:
        self.find_calls.append((binary_name, env))
        return f"/usr/bin/{binary_name}"

    def check_binary(
        self,
        binary_path: str,
        env: dict[str, str],
        *,
        timeout: int,
    ) -> None:
        self.check_calls.append((binary_path, env, timeout))

    def run(self, request: CommandRequest, sink: CommandStreamSink) -> CommandResult:
        self.run_calls.append(request)
        if self.run_error is not None:
            raise self.run_error
        for line in self.stdout:
            sink.stdout(line)
        for line in self.stderr:
            sink.stderr(line)
        return CommandResult(
            returncode=self.returncode,
            stdout="".join(self.stdout),
            stderr="".join(self.stderr),
        )


class DummyAgent(CLICodingAgent):
    def __init__(self, executor: RecordingExecutor):
        super().__init__("dummy", executor=executor)

    def _get_command(self, prompt: str) -> list[str]:
        return [self.binary_path, "run"]


def test_agent_initialization_uses_command_executor():
    executor = RecordingExecutor()

    agent = DummyAgent(executor)

    assert agent.binary_path == "/usr/bin/dummy"
    assert executor.find_calls[0][0] == "dummy"
    assert executor.check_calls == [("/usr/bin/dummy", agent.env, 10)]


def test_generate_routes_command_through_executor():
    executor = RecordingExecutor(stdout=["hello\n", "world\n"])
    agent = DummyAgent(executor)

    result = agent.generate(
        "prompt text",
        cwd="/tmp/workspace",
        timeout=123,
        silent=True,
    )

    assert result == "hello\nworld"
    call = executor.run_calls[0]
    assert call.argv == ["/usr/bin/dummy", "run"]
    assert call.stdin == "prompt text"
    assert call.cwd == "/tmp/workspace"
    assert call.env is agent.env
    assert call.timeout == 123


def test_generate_raises_on_nonzero_executor_result():
    executor = RecordingExecutor(stderr=["bad\n"], returncode=17)
    agent = DummyAgent(executor)

    with pytest.raises(RuntimeError, match="exited with code 17: bad"):
        agent.generate("prompt", silent=True)


def test_generate_propagates_executor_timeout():
    timeout = subprocess.TimeoutExpired(cmd=["dummy"], timeout=1)
    executor = RecordingExecutor(run_error=timeout)
    agent = DummyAgent(executor)

    with pytest.raises(subprocess.TimeoutExpired):
        agent.generate("prompt", timeout=1, silent=True)


def test_multiple_generates_reuse_executor():
    executor = RecordingExecutor()
    agent = DummyAgent(executor)

    for idx in range(3):
        assert "executor output" in agent.generate(f"prompt {idx}", silent=True)

    assert len(executor.run_calls) == 3


@pytest.mark.parametrize(
    ("binary_name", "agent_class"),
    [
        ("claude", ClaudeCodeCodingAgent),
        ("codex", CodexCodingAgent),
        ("gemini", GeminiCodingAgent),
    ],
)
def test_provider_agents_accept_command_executor(binary_name, agent_class):
    executor = RecordingExecutor()

    agent = agent_class(executor=executor)

    assert agent.binary_path == f"/usr/bin/{binary_name}"
    assert executor.find_calls[0][0] == binary_name
