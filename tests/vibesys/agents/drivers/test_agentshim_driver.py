"""Tests for the AgentShim driver against the library's public API.

Every turn is scripted with :func:`agentshim.testing.scripted_turn`, which
emits the provider's real stream format, so nothing here hand-writes provider
JSON or reaches into ``agentshim.core``/``agentshim.providers``. The assertions
are about VibeSys policy: what argv the provider was launched with, which
events the observer saw, how usage maps onto the neutral contract, and when a
conversation is retired.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import agentshim
import pytest
from agentshim.testing import FakeExecutor, FakeRun, TokenUsage, scripted_turn

from vibesys.agents import docker_executor
from vibesys.agents.contracts import (
    AgentEvent,
    AgentEventKind,
    AgentExecutionPolicy,
    AgentSessionSpec,
    AgentTurnRequest,
    MCPServerSpec,
    SessionDisposition,
)
from vibesys.agents.drivers import agentshim as subject
from vibesys.run.events import CommandResultPayload
from vibesys.schemas import ImplementerResponse, JudgeResponse
from vs_sandbox import HostResource, ProjectPathPolicy

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from vibesys.agents.contracts import AgentSession

SCRIPTED_PROVIDERS = ("claude", "codex", "gemini", "opencode")
"""Every provider VibeSys ships, each scripted in its own stream format.

The driver treats them all the same way, so these cases are parametrized
rather than written per provider. Where a provider's declared capabilities
change what the driver should do, the expectation is read from
``agentshim.get_provider(provider).profile`` instead of being branched on the
provider name.
"""

CACHE_WRITE_PROVIDERS = ("claude", "opencode")
"""Providers whose stream reports cache creation apart from cache reads.

Codex and Gemini print one cached-token total, so no scripted turn can carry a
separate cache-write count through them. The neutral usage contract keeps the
two fields distinct regardless; these are the providers that can fill both.
"""

RESUME_FAILURE_STDERR = {
    "claude": "no conversation found\n",
    "codex": "thread/resume failed: no rollout found for thread id thread-1\n",
    # Gemini and opencode print nothing that separates a refused resume from
    # any other startup failure, which is why agentshim applies the Claude
    # rule to them: any nonzero exit of a resumed turn is a lost conversation.
    "gemini": "fatal: failed to start\n",
    "opencode": "error: failed to start\n",
}
"""How each provider's CLI reports a resume it will not honour."""


@dataclass
class _Observer:
    events: list[AgentEvent] = field(default_factory=list)

    def on_event(self, event: AgentEvent) -> None:
        self.events.append(event)

    def kinds(self) -> list[AgentEventKind]:
        return [event.kind for event in self.events]

    def of_kind(self, kind: AgentEventKind) -> list[AgentEvent]:
        return [event for event in self.events if event.kind is kind]


class _StubSandbox:
    """A confinement policy that only records that it was applied."""

    def wrap(self, argv: list[str]) -> list[str]:
        return ["/usr/bin/stub-sandbox", "--", *argv]


@pytest.fixture
def sandbox_builds(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every host-sandbox build and leave the fake executor unconfined."""
    builds: list[dict[str, Any]] = []

    def build(workspace: Path, **kwargs: Any) -> None:  # noqa: ANN401
        builds.append({"workspace": workspace, **kwargs})

    monkeypatch.setattr(subject, "build_host_sandbox", build)
    return builds


def _spec(tmp_path: Path, **changes: Any) -> AgentSessionSpec:  # noqa: ANN401
    values: dict[str, Any] = {
        "role": "implementer",
        "provider": "claude",
        "workspace": tmp_path,
        "model": "gpt-test",
        "policy": AgentExecutionPolicy(require_enforcement=True),
        "environment": (("GPU", "0"),),
        "reasoning_effort": "high",
    }
    values.update(changes)
    return AgentSessionSpec(**values)


def _driver(
    provider: str,
    runs: FakeRun | Sequence[FakeRun] | Callable[[agentshim.CommandRequest], FakeRun],
    *,
    timeout: int | None = None,
    log: Callable[[str], None] | None = None,
    confined: bool = False,
) -> tuple[subject.AgentShimDriver, FakeExecutor]:
    """Build a driver whose provider process is the scripted fake executor."""
    fake = FakeExecutor(runs)

    def factory(sandbox: Any) -> agentshim.CommandExecutor:  # noqa: ANN401
        if confined and sandbox is not None:
            return subject.confine_to_sandbox(fake, sandbox)
        return fake

    driver = subject.AgentShimDriver(
        provider=provider,
        timeout=timeout,
        log=log,
        executor_factory=factory,
    )
    return driver, fake


def _session(
    tmp_path: Path,
    provider: str,
    runs: FakeRun | Sequence[FakeRun] | Callable[[agentshim.CommandRequest], FakeRun],
    **kwargs: Any,  # noqa: ANN401
) -> tuple[AgentSession, FakeExecutor]:
    driver, fake = _driver(provider, runs, **kwargs)
    return driver.create_session(_spec(tmp_path, provider=provider)), fake


# ---------------------------------------------------------------------------
# One turn: result, events, launch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_turn_reports_its_text_conversation_and_usage(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    del sandbox_builds
    session, _fake = _session(
        tmp_path,
        provider,
        scripted_turn(
            provider,
            text="done",
            session_id="session-1",
            usage=TokenUsage(
                input_tokens=1200,
                output_tokens=30,
                cached_input_tokens=1000,
            ),
        ),
    )

    result = session.run_turn(AgentTurnRequest(message="Do it", instructions="Rules"))

    assert result.text == "done"
    assert result.provider_session_id == "session-1"
    assert result.disposition is SessionDisposition.REUSABLE
    # Cached tokens are part of the input total on every provider.
    assert result.usage.input_tokens == 1200
    assert result.usage.output_tokens == 30
    assert result.usage.cache_read_input_tokens == 1000
    assert result.usage.duration_ms is not None


@pytest.mark.parametrize("provider", CACHE_WRITE_PROVIDERS)
def test_cache_creation_keeps_its_own_field(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    """Writing the cache and reading it are separate costs, not one number."""
    del sandbox_builds
    session, _fake = _session(
        tmp_path,
        provider,
        scripted_turn(
            provider,
            text="done",
            usage=TokenUsage(
                input_tokens=1200,
                output_tokens=30,
                cached_input_tokens=1000,
                cache_write_input_tokens=200,
            ),
        ),
    )

    result = session.run_turn(AgentTurnRequest(message="Do it"))

    assert result.usage.input_tokens == 1200
    assert result.usage.cache_read_input_tokens == 1000
    assert result.usage.cache_creation_input_tokens == 200


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_the_launch_carries_the_prompt_model_workspace_and_environment(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    del sandbox_builds
    session, fake = _session(tmp_path, provider, scripted_turn(provider, text="ok"), timeout=30)

    session.run_turn(
        AgentTurnRequest(
            message="Do it",
            instructions="Follow these rules",
            timeout=timedelta(seconds=7),
        )
    )

    request = fake.requests[-1]
    # The prompt is delivered on stdin, never in argv.
    assert request.stdin == "Follow these rules\n\nDo it"
    assert not any("Follow these rules" in argument for argument in request.argv)
    assert "--model" in request.argv
    assert request.argv[request.argv.index("--model") + 1] == "gpt-test"
    assert request.cwd == str(tmp_path)
    assert request.timeout == 7
    # The session environment overlay reaches the provider process.
    assert request.env["GPU"] == "0"


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_the_session_timeout_applies_when_a_turn_names_none(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    del sandbox_builds
    session, fake = _session(tmp_path, provider, scripted_turn(provider, text="ok"), timeout=30)

    session.run_turn(AgentTurnRequest(message="Do it"))

    assert fake.requests[-1].timeout == 30


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_the_reasoning_effort_reaches_a_provider_that_supports_it(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    del sandbox_builds
    session, fake = _session(tmp_path, provider, scripted_turn(provider, text="ok"))

    session.run_turn(AgentTurnRequest(message="Do it"))

    profile = agentshim.get_provider(provider).profile
    # Codex carries the level inside a ``--config`` assignment rather than as
    # its own argument, so the whole command line is what is searched.
    launched = " ".join(fake.requests[-1].argv)
    assert ("high" in launched) is profile.supports_reasoning_effort


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_the_turn_streams_neutral_events(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    del sandbox_builds
    session, _fake = _session(
        tmp_path,
        provider,
        scripted_turn(
            provider,
            text="answering",
            session_id="session-1",
            tool_calls=[("shell", {"command": "cargo test"}, "ok")],
        ),
    )
    observer = _Observer()

    session.run_turn(AgentTurnRequest(message="Do it"), observer)

    kinds = observer.kinds()
    assert AgentEventKind.TEXT in kinds
    assert kinds.index(AgentEventKind.TOOL_CALL) < kinds.index(AgentEventKind.TOOL_RESULT)
    assert kinds[-1] is AgentEventKind.USAGE
    call = observer.of_kind(AgentEventKind.TOOL_CALL)[0]
    # Each CLI names its own shell tool (Codex calls it ``execute``), so the
    # neutral event carries whatever the provider reported; what has to
    # survive translation is that a name and the arguments are both there.
    assert call.payload["tool"]
    assert call.payload["args"] == {"command": "cargo test"}
    result = observer.of_kind(AgentEventKind.TOOL_RESULT)[0]
    assert result.text == "ok"
    # The flat fields remain for consumers that predate the typed payload, and
    # the typed payload restates exactly them. Which of them a provider fills
    # differs (only Codex reports the command's exit status), so the two views
    # are compared against each other rather than against fixed values.
    payload = result.payload["result_payload"]
    assert isinstance(payload, CommandResultPayload)
    assert payload.stdout == result.payload["stdout"]
    assert payload.stderr == result.payload["stderr"]
    assert payload.exit_code == result.payload["exit_code"]
    assert payload.duration == result.payload["duration"]
    assert result.payload["stdout"] == "ok"
    assert result.payload["stderr"] == ""


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_every_tool_call_is_forwarded_as_its_own_pair(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    """Two identical calls are two events: the driver never deduplicates."""
    del sandbox_builds
    session, _fake = _session(
        tmp_path,
        provider,
        scripted_turn(
            provider,
            text="done",
            tool_calls=[
                ("Read", {"path": "a.rs"}, "contents"),
                ("Read", {"path": "a.rs"}, "contents"),
            ],
        ),
    )
    observer = _Observer()

    session.run_turn(AgentTurnRequest(message="Do it"), observer)

    assert len(observer.of_kind(AgentEventKind.TOOL_CALL)) == 2
    assert len(observer.of_kind(AgentEventKind.TOOL_RESULT)) == 2


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_provider_plumbing_is_marked_for_the_diagnostic_channel(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    """A stderr line is not the agent's chain of thought."""
    del sandbox_builds
    scripted = scripted_turn(provider, text="done", session_id="session-1")
    session, _fake = _session(
        tmp_path,
        provider,
        FakeRun(stdout=scripted.stdout, stderr=["warning: slow filesystem\n"]),
    )
    observer = _Observer()

    session.run_turn(AgentTurnRequest(message="Do it"), observer)

    plumbing = [
        event
        for event in observer.of_kind(AgentEventKind.THINKING)
        if event.payload.get("channel") == "diagnostic"
    ]
    assert any("slow filesystem" in (event.text or "") for event in plumbing)
    # The subprocess lifecycle itself is not agent-visible output.
    assert not any("RunStarted" in (event.text or "") for event in observer.events)


# ---------------------------------------------------------------------------
# Confinement and host resources
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_the_host_sandbox_wraps_the_provider_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
) -> None:
    monkeypatch.setattr(subject, "build_host_sandbox", lambda *_a, **_k: _StubSandbox())
    driver, fake = _driver(provider, scripted_turn(provider, text="ok"), confined=True)
    session = driver.create_session(_spec(tmp_path, provider=provider))

    session.run_turn(AgentTurnRequest(message="Do it"))

    argv = list(fake.requests[-1].argv)
    assert argv[0] == "/usr/bin/stub-sandbox"
    assert argv[argv.index("--") + 1].endswith(agentshim.get_provider(provider).profile.binary)


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_an_unconfined_host_runs_the_provider_binary_directly(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    """``build_host_sandbox`` returns ``None`` where confinement is unavailable."""
    del sandbox_builds
    session, fake = _session(tmp_path, provider, scripted_turn(provider, text="ok"), confined=True)

    session.run_turn(AgentTurnRequest(message="Do it"))

    assert fake.requests[-1].argv[0].endswith(agentshim.get_provider(provider).profile.binary)


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_declared_host_resources_reach_the_sandbox(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    """A caller's grant must survive the provider's default declaration."""
    resource = HostResource(tmp_path / "toolchain", purpose="test toolchain")
    policy = ProjectPathPolicy(read_only_paths=("OBJECTIVE.md",))
    driver, _fake = _driver(provider, scripted_turn(provider, text="ok"))

    driver.create_session(
        _spec(
            tmp_path,
            provider=provider,
            policy=AgentExecutionPolicy(
                project_paths=policy,
                host_resources=(resource,),
                require_enforcement=True,
            ),
        )
    )

    build = sandbox_builds[0]
    assert build["workspace"] == tmp_path
    assert build["project_path_policy"] is policy
    assert build["require_enforcement"] is True
    assert resource in build["resources"]
    # The sandbox sees the session's environment overlay, not just the shell's.
    assert build["env"]["GPU"] == "0"


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


def test_mcp_servers_are_installed_for_the_turn_and_removed_after(tmp_path: Path) -> None:
    """The config exists only while the provider process is running.

    Claude discovers MCP servers from ``<workspace>/.mcp.json``, so the file is
    read from inside the scripted run: that is the only moment it exists.
    """
    observed: list[dict[str, Any]] = []

    def run(_request: agentshim.CommandRequest) -> FakeRun:
        observed.append(json.loads((tmp_path / ".mcp.json").read_text()))
        return scripted_turn("claude", text="ok")

    driver, _fake = _driver("claude", run)
    session = driver.create_session(
        _spec(
            tmp_path,
            mcp_servers=(MCPServerSpec(name="issues", command="python", args=("-m", "issues")),),
        )
    )

    session.run_turn(AgentTurnRequest(message="review"))

    entry = observed[0]["mcpServers"]["issues"]
    # A host run pins the interpreter that VibeSys itself is running under: a
    # login shell's bare ``python`` may not have the MCP dependencies.
    assert entry["command"] == subject.sys.executable
    assert entry["args"] == ["-m", "issues"]
    assert not (tmp_path / ".mcp.json").exists()


def test_a_non_python_mcp_command_is_left_alone(tmp_path: Path) -> None:
    observed: list[dict[str, Any]] = []

    def run(_request: agentshim.CommandRequest) -> FakeRun:
        observed.append(json.loads((tmp_path / ".mcp.json").read_text()))
        return scripted_turn("claude", text="ok")

    driver, _fake = _driver("claude", run)
    session = driver.create_session(
        _spec(
            tmp_path,
            mcp_servers=(MCPServerSpec(name="other", command="node", args=("server.js",)),),
        )
    )

    session.run_turn(AgentTurnRequest(message="review"))

    assert observed[0]["mcpServers"]["other"]["command"] == "node"


# ---------------------------------------------------------------------------
# Container execution
# ---------------------------------------------------------------------------


def _workspace_files(root: Path) -> dict[str, str]:
    """Every file under *root* by relative path, with its text.

    Each config-file provider names its own file (`.mcp.json`, `opencode.json`,
    `.gemini/settings.json`), so the workspace is read whole rather than
    branched on the provider.
    """
    return {
        str(path.relative_to(root)): path.read_text()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _container_driver(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    runs: FakeRun | Sequence[FakeRun] | Callable[[agentshim.CommandRequest], FakeRun],
) -> tuple[subject.AgentShimDriver, FakeExecutor, list[tuple[str, int, int]]]:
    """Build a container-mode driver whose ``docker`` client is the fake.

    The Docker executor rewrites each request into a ``docker exec`` command
    and hands it to a host executor; substituting that inner executor is what
    lets the argv the container would have received be asserted directly.
    """
    fake = FakeExecutor(runs)
    repairs: list[tuple[str, int, int]] = []

    monkeypatch.setattr(docker_executor, "HostCommandExecutor", lambda: fake)
    monkeypatch.setattr(
        docker_executor,
        "repair_workspace_ownership",
        lambda container_id, *, uid, gid: repairs.append((container_id, uid, gid)),
    )
    driver = subject.AgentShimDriver(
        provider=provider,
        docker_sandboxes={"implementer": SimpleNamespace(container_id="container-1")},
    )
    return driver, fake, repairs


def _container_spec(tmp_path: Path, provider: str, **changes: Any) -> AgentSessionSpec:  # noqa: ANN401
    return _spec(
        tmp_path,
        provider=provider,
        policy=AgentExecutionPolicy(containerized=True),
        **changes,
    )


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_container_turn_carries_the_session_environment_and_workdir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
) -> None:
    """The overlay reaches the CLI inside the container, not the docker client.

    A container starts from its image's environment, so the session overlay has
    to be handed to ``docker exec`` as ``-e`` flags; and the container turn
    names no ``cwd`` of its own, so the executor supplies ``-w``.
    """
    driver, fake, _repairs = _container_driver(
        monkeypatch, provider, scripted_turn(provider, text="ok")
    )
    session = driver.create_session(_container_spec(tmp_path, provider))

    session.run_turn(AgentTurnRequest(message="Do it"))

    argv = list(fake.requests[-1].argv)
    assert argv[:3] == ["docker", "exec", "-i"]
    assert argv[argv.index("-w") + 1] == docker_executor.DEFAULT_CONTAINER_WORKDIR
    forwarded = [argv[index + 1] for index, item in enumerate(argv) if item == "-e"]
    assert "GPU=0" in forwarded
    # The host paths agentshim assembled for this process must not cross over:
    # they name directories that do not exist inside the container.
    assert not any(entry.startswith("PATH=") for entry in forwarded)
    assert "container-1" in argv
    assert argv[argv.index("container-1") + 1].endswith(
        agentshim.get_provider(provider).profile.binary
    )
    # The turn's own working directory stays unset: `-w` is what carries it.
    assert fake.requests[-1].cwd is None


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_container_turn_carries_no_host_device_pin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
) -> None:
    """The host GPU index is not a fact about the inside of the container.

    An editor container is started with ``--gpus device=N``, which makes the
    chosen GPU device 0 inside it, so the host's ``CUDA_VISIBLE_DEVICES``
    would name a device that is not there. The run context keeps the pin out
    of the session spec in container mode; the driver's part of that contract
    is that it forwards only what the spec declared and never reads the
    variable off the host environment.
    """
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "3")
    driver, fake, _repairs = _container_driver(
        monkeypatch, provider, scripted_turn(provider, text="ok")
    )
    session = driver.create_session(_container_spec(tmp_path, provider, environment=()))

    session.run_turn(AgentTurnRequest(message="Do it"))

    argv = list(fake.requests[-1].argv)
    forwarded = [argv[index + 1] for index, item in enumerate(argv) if item == "-e"]
    assert forwarded == []
    assert not any("CUDA_VISIBLE_DEVICES" in argument for argument in argv)


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_container_turn_repairs_workspace_ownership_afterwards(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
) -> None:
    """CLI agents run as root in the container; the host user gets its files back."""
    driver, _fake, repairs = _container_driver(
        monkeypatch, provider, scripted_turn(provider, text="ok")
    )
    session = driver.create_session(_container_spec(tmp_path, provider))

    session.run_turn(AgentTurnRequest(message="Do it"))

    assert repairs == [("container-1", os.getuid(), os.getgid())]


def test_a_container_codex_run_is_watched_for_a_stalled_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A resumed containerized ``codex exec --json`` can finish without exiting."""
    driver, _fake, _repairs = _container_driver(
        monkeypatch, "codex", scripted_turn("codex", text="ok")
    )
    session = driver.create_session(_container_spec(tmp_path, "codex"))

    assert isinstance(
        driver._container_executor(_container_spec(tmp_path, "codex")),  # noqa: SLF001
        docker_executor.CodexRolloutWatchdogExecutor,
    )
    session.close()


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_container_session_mcp_servers_are_installed_on_the_host_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
) -> None:
    """A container turn writes its MCP config on the bind-mounted host workspace.

    The CLI runs at the container's own path, so the turn names no host
    working directory for agentshim to derive the config directory from. The
    driver points ``mcp_workspace`` at the host workspace instead, which the
    container sees as ``/workspace``. As on the host, the file exists only
    while the provider process is running, so the scripted run is the moment
    it can be observed.
    """
    server = MCPServerSpec(name="issues", command="python", args=("-m", "issues"))
    during: list[dict[str, str]] = []

    def run(_request: agentshim.CommandRequest) -> FakeRun:
        # Every container command is scripted, the provider health check
        # included, so the turn is the last snapshot rather than the only one.
        during.append(_workspace_files(tmp_path))
        return scripted_turn(provider, text="ok")

    driver, fake, _repairs = _container_driver(monkeypatch, provider, run)
    session = driver.create_session(_container_spec(tmp_path, provider, mcp_servers=(server,)))

    session.run_turn(AgentTurnRequest(message="review"))

    if agentshim.get_provider(provider).profile.mcp is not agentshim.McpMechanism.CONFIG_FILE:
        # Codex passes its servers as `--config` flags, so nothing is written
        # to the workspace in either execution mode.
        assert during[-1] == {}
        assert 'mcp_servers.issues.command="python"' in fake.requests[-1].argv
        return

    written = during[-1]
    assert written, f"{provider} wrote no MCP config on the host workspace"
    config = "".join(written.values())
    assert "issues" in config
    # The container image resolves its own interpreter: the host substitution
    # would name a path that does not exist inside it.
    assert subject.sys.executable not in config
    assert '"python"' in config
    assert _workspace_files(tmp_path) == {}, "the MCP config outlived the turn"


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_native_schema_replaces_the_prompt_contract(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    del sandbox_builds
    profile = agentshim.get_provider(provider).profile
    native = profile.output_schema is not agentshim.OutputSchemaStyle.NONE
    payload = {"analysis": "it improved", "verdict": "accept"}
    logs: list[str] = []
    session, fake = _session(
        tmp_path,
        provider,
        # A provider with no schema flag answers in prose, so the payload has
        # to arrive as the message text instead of out of band.
        scripted_turn(
            provider,
            text="" if native else json.dumps(payload),
            structured_output=payload if native else None,
        ),
        log=logs.append,
    )

    result = session.run_turn(
        AgentTurnRequest(message="usr", instructions="sys", output_schema=JudgeResponse)
    )

    # The prompt contract is the portable fallback: it appears exactly where
    # the provider has no native schema of its own.
    contract_in_prompt = "Schema for JudgeResponse" in (fake.requests[-1].stdin or "")
    assert contract_in_prompt is not native
    assert any("using prompt fallback" in message for message in logs) is not native
    # The caller parses the answer back into its response model, so the
    # schema-conformant payload is what the turn reports as its text.
    assert json.loads(result.text) == payload


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_structured_turn_reports_its_payload_off_the_assistant_channel(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    """The schema payload is an answer to parse, not prose to stream.

    The caller renders a structured turn from the parsed response model, so
    streaming the raw JSON as assistant text would show the answer twice, once
    unformatted. It still reaches the diagnostic channel, where the raw
    provider output belongs.
    """
    del sandbox_builds
    profile = agentshim.get_provider(provider).profile
    payload = {"analysis": "it improved", "verdict": "accept"}
    native = profile.output_schema is not agentshim.OutputSchemaStyle.NONE
    session, _fake = _session(
        tmp_path,
        provider,
        scripted_turn(
            provider,
            text=json.dumps(payload),
            structured_output=payload if native else None,
        ),
    )
    observer = _Observer()

    result = session.run_turn(
        AgentTurnRequest(message="usr", instructions="sys", output_schema=JudgeResponse),
        observer,
    )

    assert json.loads(result.text) == payload
    assert observer.of_kind(AgentEventKind.TEXT) == []
    diagnostics = [
        event.text or ""
        for event in observer.of_kind(AgentEventKind.THINKING)
        if event.payload.get("channel") == "diagnostic"
    ]
    assert any(json.dumps(payload) in text for text in diagnostics)


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_later_plain_turn_streams_its_answer_again(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    """Suppressing assistant text is the structured turn's rule, not the session's."""
    del sandbox_builds
    session, _fake = _session(
        tmp_path,
        provider,
        [
            scripted_turn(provider, text="{}"),
            scripted_turn(provider, text="prose"),
        ],
    )

    session.run_turn(
        AgentTurnRequest(message="usr", instructions="sys", output_schema=JudgeResponse)
    )
    observer = _Observer()
    session.run_turn(AgentTurnRequest(message="usr", instructions="sys"), observer)

    assert [event.text for event in observer.of_kind(AgentEventKind.TEXT)] == ["prose"]


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_mapping_response_stays_native_where_the_dialect_allows_it(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    """``ImplementerResponse.metrics`` is a ``dict[str, float]``."""
    del sandbox_builds
    profile = agentshim.get_provider(provider).profile
    if profile.schema_dialect is not agentshim.SchemaDialect.OPEN:
        pytest.skip(f"{provider} cannot express an open object map natively")
    session, fake = _session(tmp_path, provider, scripted_turn(provider, text="{}"))

    session.run_turn(
        AgentTurnRequest(message="usr", instructions="sys", output_schema=ImplementerResponse)
    )

    assert "Schema for ImplementerResponse" not in (fake.requests[-1].stdin or "")


def test_a_mapping_response_falls_back_on_a_strict_provider(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    """Codex's ``--output-schema`` subset cannot express an open object map."""
    del sandbox_builds
    logs: list[str] = []
    session, fake = _session(tmp_path, "codex", scripted_turn("codex", text="{}"), log=logs.append)

    session.run_turn(
        AgentTurnRequest(message="usr", instructions="sys", output_schema=ImplementerResponse)
    )

    assert "Schema for ImplementerResponse" in (fake.requests[-1].stdin or "")
    assert any("using prompt fallback" in message for message in logs)


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_schema_no_dialect_accepts_falls_back_to_the_prompt_contract(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    """An expensive turn must not be spent discovering the CLI rejects a schema."""
    del sandbox_builds

    class UnsupportedResponse(JudgeResponse):
        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401, ARG003
            return {
                "type": "object",
                "properties": {"analysis": {"type": "string"}},
                "not": {"required": ["analysis"]},
            }

    logs: list[str] = []
    session, fake = _session(
        tmp_path, provider, scripted_turn(provider, text="{}"), log=logs.append
    )

    session.run_turn(
        AgentTurnRequest(message="usr", instructions="sys", output_schema=UnsupportedResponse)
    )

    assert "Schema for UnsupportedResponse" in (fake.requests[-1].stdin or "")
    assert any("using prompt fallback" in message for message in logs)


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_plain_turn_carries_no_response_contract(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    """A reused session must not inherit the previous turn's schema."""
    del sandbox_builds
    session, fake = _session(
        tmp_path,
        provider,
        [scripted_turn(provider, text="{}"), scripted_turn(provider, text="prose")],
    )

    session.run_turn(
        AgentTurnRequest(message="usr", instructions="sys", output_schema=JudgeResponse)
    )
    result = session.run_turn(AgentTurnRequest(message="usr", instructions="sys"))

    assert result.text == "prose"
    assert "Schema for JudgeResponse" not in (fake.requests[1].stdin or "")
    # A native schema rides in argv and a fallback contract rides in the
    # prompt, so which channel shrinks depends on the provider; neither may
    # carry anything over into the plain turn.
    profile = agentshim.get_provider(provider).profile
    if profile.output_schema is agentshim.OutputSchemaStyle.NONE:
        assert len(fake.requests[0].argv) == len(fake.requests[1].argv)
    else:
        assert len(fake.requests[0].argv) > len(fake.requests[1].argv)


# ---------------------------------------------------------------------------
# Conversation lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_the_second_turn_continues_the_first_conversation(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    del sandbox_builds
    session, fake = _session(
        tmp_path, provider, scripted_turn(provider, text="ok", session_id="session-1")
    )

    session.run_turn(AgentTurnRequest(message="one"))
    session.run_turn(AgentTurnRequest(message="two"))

    assert "session-1" not in fake.requests[0].argv
    assert "session-1" in fake.requests[1].argv


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_checkpoint_is_adopted_only_while_no_conversation_is_live(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    del sandbox_builds
    session, fake = _session(
        tmp_path, provider, scripted_turn(provider, text="ok", session_id="session-1")
    )

    assert session.resume_provider_session("checkpoint") is True
    session.run_turn(AgentTurnRequest(message="one"))
    # The live conversation is newer than any checkpoint the caller holds.
    assert session.resume_provider_session("older-checkpoint") is False

    session.run_turn(AgentTurnRequest(message="two"))
    assert "checkpoint" in fake.requests[0].argv
    assert "session-1" in fake.requests[1].argv


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_failed_resume_retries_once_from_a_fresh_conversation(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    """A conversation the provider will not resume must not kill the run."""
    del sandbox_builds

    attempts: list[int] = []
    refusal = RESUME_FAILURE_STDERR[provider]

    def run(request: agentshim.CommandRequest) -> FakeRun:
        attempts.append(len(attempts))
        if not attempts[:-1]:
            return scripted_turn(provider, text="ok", session_id="session-1")
        if "session-1" in request.argv:
            return FakeRun(returncode=1, stderr=[refusal])
        return scripted_turn(provider, text="recovered", session_id="session-2")

    session, fake = _session(tmp_path, provider, run)
    session.run_turn(AgentTurnRequest(message="one"))

    result = session.run_turn(AgentTurnRequest(message="two"))

    assert result.text == "recovered"
    assert result.disposition is SessionDisposition.RESET_REQUIRED
    assert "session-1" in fake.requests[1].argv
    assert "session-1" not in fake.requests[2].argv


def test_a_resumed_turn_that_fails_otherwise_drops_the_conversation(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    """A resumed turn that raises must not leave the conversation to be retried.

    Codex keeps a generic ``CliExitError`` for a resumed turn that failed for
    a reason other than a missing rollout, so this is the case the retry path
    cannot serve. A raise carries no ``AgentTurnResult``, so the turn cannot
    report ``RESET_REQUIRED`` either; forgetting the conversation is what stops
    every later turn from resuming history the provider already refused once.
    """
    del sandbox_builds

    def run(request: agentshim.CommandRequest) -> FakeRun:
        if "thread-1" in request.argv:
            return FakeRun(returncode=1, stderr=["boom\n"])
        return scripted_turn("codex", text="ok", session_id="thread-1")

    session, fake = _session(tmp_path, "codex", run)
    session.run_turn(AgentTurnRequest(message="one"))

    with pytest.raises(agentshim.CliExitError) as raised:
        session.run_turn(AgentTurnRequest(message="two"))

    assert not isinstance(raised.value, agentshim.SessionResumeError)
    assert "thread-1" in fake.requests[1].argv
    # The conversation was dropped, so the next turn starts fresh.
    session.run_turn(AgentTurnRequest(message="three"))
    assert "thread-1" not in fake.requests[2].argv


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_fresh_turn_that_fails_is_not_retried(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    """Nothing was resumed, so the failure is the agent's own."""
    del sandbox_builds
    session, fake = _session(tmp_path, provider, FakeRun(returncode=1, stderr=["boom\n"]))

    with pytest.raises(agentshim.CliExitError):
        session.run_turn(AgentTurnRequest(message="one"))

    assert len(fake.requests) == 1


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_turn_that_times_out_is_reported_as_a_subprocess_timeout(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    """Loops fail closed on ``subprocess.TimeoutExpired`` and read its budget."""
    del sandbox_builds
    session, _fake = _session(tmp_path, provider, FakeRun(timeout=True), timeout=45)

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        session.run_turn(AgentTurnRequest(message="one"))

    assert raised.value.timeout == 45


def test_the_codex_thread_budget_retires_a_conversation(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    del sandbox_builds
    session, _fake = _session(
        tmp_path, "codex", scripted_turn("codex", text="ok", session_id="thread-1")
    )

    first = session.run_turn(AgentTurnRequest(message="one"))
    second = session.run_turn(AgentTurnRequest(message="two"))

    assert first.disposition is SessionDisposition.REUSABLE
    # The budget is spent, so the thread is retired and the caller is told the
    # conversation this session named no longer exists.
    assert second.disposition is SessionDisposition.RESET_REQUIRED
    assert second.provider_session_id == "thread-1"


def test_a_heavy_codex_turn_retires_its_conversation(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    del sandbox_builds
    session, _fake = _session(
        tmp_path,
        "codex",
        scripted_turn(
            "codex",
            text="ok",
            session_id="thread-1",
            usage=TokenUsage(input_tokens=20_000_000),
        ),
    )

    result = session.run_turn(AgentTurnRequest(message="one"))

    assert result.disposition is SessionDisposition.RESET_REQUIRED


# ---------------------------------------------------------------------------
# Cancellation and lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_cancel_stops_the_running_provider_process(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    del sandbox_builds
    session, fake = _session(tmp_path, provider, scripted_turn(provider, text="ok"))
    reached = threading.Event()

    class _HoldingObserver:
        def on_event(self, event: AgentEvent) -> None:
            del event
            if reached.is_set():
                return
            reached.set()
            # Hold the turn open until the canceller reaches the process handle.
            deadline = time.monotonic() + 5
            while not fake.handles[-1].terminated and time.monotonic() < deadline:
                time.sleep(0.01)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        turn = pool.submit(session.run_turn, AgentTurnRequest(message="one"), _HoldingObserver())
        assert reached.wait(5)
        session.cancel()
        turn.result(timeout=5)

    assert fake.handles[-1].terminated
    # The turn unwound on its own, so the harder stop was never needed.
    assert not fake.handles[-1].killed


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_independent_sessions_run_concurrently(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    """Closing one session must not interrupt another session's turn."""
    del sandbox_builds
    barrier = threading.Barrier(2)
    release_optimizer = threading.Event()

    def run(request: agentshim.CommandRequest) -> FakeRun:
        barrier.wait(timeout=5)
        if request.env.get("CHAT_MODE") == "read-only":
            return scripted_turn(provider, text="chat result")
        assert release_optimizer.wait(timeout=5)
        return scripted_turn(provider, text="optimizer result")

    driver, _fake = _driver(provider, run)
    optimizer = driver.create_session(_spec(tmp_path, provider=provider, role="implementer"))
    chat = driver.create_session(
        _spec(
            tmp_path,
            provider=provider,
            role="chat",
            environment=(("CHAT_MODE", "read-only"),),
        )
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        optimizer_turn = pool.submit(optimizer.run_turn, AgentTurnRequest(message="optimize"))
        chat_turn = pool.submit(chat.run_turn, AgentTurnRequest(message="explain"))
        assert chat_turn.result(timeout=10).text == "chat result"
        chat.close()
        assert not optimizer_turn.done()
        release_optimizer.set()
        assert optimizer_turn.result(timeout=10).text == "optimizer result"

    driver.close()


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_driver_and_session_close_are_idempotent(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    del sandbox_builds
    driver, _fake = _driver(provider, scripted_turn(provider, text="ok"))
    session = driver.create_session(_spec(tmp_path, provider=provider))

    session.close()
    session.close()
    driver.close()
    driver.close()

    with pytest.raises(RuntimeError, match="closed"):
        session.run_turn(AgentTurnRequest(message="later"))
    with pytest.raises(RuntimeError, match="closed"):
        driver.create_session(_spec(tmp_path, provider=provider))


# ---------------------------------------------------------------------------
# Driver configuration
# ---------------------------------------------------------------------------


def test_the_shipped_providers_are_the_ones_the_driver_accepts() -> None:
    assert subject.supported_providers() == ["claude", "codex", "gemini", "opencode"]
    for provider in SCRIPTED_PROVIDERS:
        assert provider in subject.supported_providers()


def test_an_unknown_provider_is_rejected_by_name() -> None:
    with pytest.raises(ValueError, match="nonesuch"):
        subject.AgentShimDriver(provider="nonesuch")


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_capabilities_report_the_provider_and_execution_mode(provider: str) -> None:
    driver, _fake = _driver(provider, scripted_turn(provider, text="ok"))
    capabilities = driver.capabilities

    profile = agentshim.get_provider(provider).profile
    assert capabilities.provider_session_resume is profile.supports_resume
    assert capabilities.mcp_servers is True
    assert capabilities.host_path_grants is True
    assert capabilities.container_execution is False


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_session_spec_for_another_provider_is_rejected(
    tmp_path: Path,
    provider: str,
) -> None:
    driver, _fake = _driver(provider, scripted_turn(provider, text="ok"))
    other = next(name for name in SCRIPTED_PROVIDERS if name != provider)

    with pytest.raises(ValueError, match="cannot create"):
        driver.create_session(_spec(tmp_path, provider=other))


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_container_policy_on_a_host_driver_is_rejected(
    tmp_path: Path,
    provider: str,
) -> None:
    driver, _fake = _driver(provider, scripted_turn(provider, text="ok"))

    with pytest.raises(ValueError, match="container policy"):
        driver.create_session(
            _spec(
                tmp_path,
                provider=provider,
                policy=AgentExecutionPolicy(containerized=True),
            )
        )
