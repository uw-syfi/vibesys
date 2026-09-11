"""Tests for the AgentShim driver against the library's public API.

Every turn is scripted with :func:`agentshim.testing.scripted_turn`, which
emits the provider's real stream format, so nothing here hand-writes provider
JSON or reaches into ``agentshim.core``/``agentshim.providers``. The assertions
are about VibeSys policy: what argv the provider was launched with, which
events the observer saw, how usage maps onto the neutral contract, and when a
conversation is retired.

Sandbox-facing scenarios run against two ``WorkspaceSandbox`` doubles,
``_FakeHostSandbox`` and ``_FakeDockerSandbox``: the driver has one code path
for both (:func:`vibesys.agents.drivers.agentshim.confine_to_sandbox`), so a
test that is really about that path is parametrized over both rather than
duplicated per mode.
"""

from __future__ import annotations

import concurrent.futures
import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import agentshim
import pytest
from agentshim.testing import (
    FakeExecutor,
    FakeRun,
    TokenUsage,
    installed_mcp_servers,
    scripted_resume_failure,
    scripted_turn,
)

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


@dataclass
class _Observer:
    events: list[AgentEvent] = field(default_factory=list)

    def on_event(self, event: AgentEvent) -> None:
        self.events.append(event)

    def kinds(self) -> list[AgentEventKind]:
        return [event.kind for event in self.events]

    def of_kind(self, kind: AgentEventKind) -> list[AgentEvent]:
        return [event for event in self.events if event.kind is kind]


@dataclass
class _FakeHostSandbox:
    """A ``WorkspaceSandbox`` double shaped like the real host backends.

    ``agent_path`` is identity, matching every host backend: there is no
    remapping table, the confined process sees exactly the host path.
    """

    home: str = "/home/user"
    path: str = "/usr/bin"
    calls: list[list[str]] = field(default_factory=list)

    def wrap(self, argv: list[str], cwd: Path | str | None = None) -> list[str]:
        del cwd
        self.calls.append(list(argv))
        return ["/usr/bin/stub-sandbox", "--", *argv]

    def agent_path(self, path: Path | str) -> str:
        return str(path)

    @property
    def env(self) -> dict[str, str]:
        return {"HOME": self.home, "PATH": self.path}


@dataclass
class _FakeDockerSandbox:
    """A ``WorkspaceSandbox`` double shaped like ``vs_sandbox.DockerSandbox``.

    ``wrap`` accepts the optional ``cwd`` the real sandbox does (the
    capability :func:`confine_to_sandbox` probes for), maps it through the
    same workspace-prefix rule ``agent_path`` uses, and renders its own extra
    environment as ``-e`` flags exactly the way the real sandbox's ``wrap``
    does.
    """

    workspace: Path
    container_id: str = "container-1"
    extra_env: dict[str, str] = field(default_factory=dict)
    home: str = "/home/agent"
    container_path: str = "/usr/local/bin:/usr/bin"

    def agent_path(self, path: Path | str) -> str:
        normalized = str(path)
        workspace = str(self.workspace)
        if normalized == workspace:
            return "/workspace"
        if normalized.startswith(workspace + "/"):
            return "/workspace" + normalized[len(workspace) :]
        return normalized

    def wrap(self, argv: list[str], cwd: Path | str | None = None) -> list[str]:
        workdir = self.agent_path(cwd) if cwd is not None else "/workspace"
        env_flags = [
            flag for key, value in self.extra_env.items() for flag in ("-e", f"{key}={value}")
        ]
        return ["docker", "exec", "-i", "-w", workdir, *env_flags, self.container_id, *argv]

    @property
    def env(self) -> dict[str, str]:
        return {"HOME": self.home, "PATH": self.container_path, **self.extra_env}


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


def _driver(  # noqa: PLR0913
    provider: str,
    runs: FakeRun | Sequence[FakeRun] | Callable[[agentshim.CommandRequest], FakeRun],
    *,
    timeout: int | None = None,
    log: Callable[[str], None] | None = None,
    docker_sandboxes: dict[str, Any] | None = None,
    check_timeout: float | None = None,
) -> tuple[subject.AgentShimDriver, FakeExecutor]:
    """Build a driver whose provider process is the scripted fake executor.

    The same factory backs every mode: the driver applies
    ``confine_to_sandbox`` itself whenever it has a sandbox, so a test
    controls confinement through what ``build_host_sandbox`` returns (the
    ``sandbox_builds`` fixture below) or through ``docker_sandboxes``, never
    through the executor factory.
    """
    fake = FakeExecutor(runs)
    driver = subject.AgentShimDriver(
        provider=provider,
        timeout=timeout,
        log=log,
        docker_sandboxes=docker_sandboxes,
        check_timeout=check_timeout,
        executor_factory=lambda: fake,
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
# Confinement: one wrapping path for every sandbox
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_the_host_sandbox_wraps_the_provider_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
) -> None:
    monkeypatch.setattr(subject, "build_host_sandbox", lambda *_a, **_k: _FakeHostSandbox())
    driver, fake = _driver(provider, scripted_turn(provider, text="ok"))
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
    session, fake = _session(tmp_path, provider, scripted_turn(provider, text="ok"))

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


@pytest.mark.parametrize("sandbox_kind", ["host", "docker"])
def test_confine_to_sandbox_rewrites_argv_through_any_workspace_sandbox(
    tmp_path: Path,
    sandbox_kind: str,
) -> None:
    """One transform serves a host confinement policy and a Docker sandbox alike."""
    sandbox = (
        _FakeHostSandbox() if sandbox_kind == "host" else _FakeDockerSandbox(workspace=tmp_path)
    )
    fake = FakeExecutor(scripted_turn("claude", text="ok"))

    executor = subject.confine_to_sandbox(fake, sandbox)
    executor.run(
        agentshim.CommandRequest(
            argv=["claude", "-p"], stdin=None, cwd=str(tmp_path), env={}, timeout=None
        ),
        agentshim.NullSink(),
    )

    argv = list(fake.requests[-1].argv)
    if isinstance(sandbox, _FakeDockerSandbox):
        assert argv[:3] == ["docker", "exec", "-i"]
        assert argv[argv.index("-w") + 1] == "/workspace"
    else:
        assert argv[0] == "/usr/bin/stub-sandbox"


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("sandbox_builds")
def test_mcp_servers_are_installed_for_the_turn_and_removed_after(tmp_path: Path) -> None:
    """The config exists only while the provider process is running.

    Claude discovers MCP servers from a workspace config file, so it is read
    with ``installed_mcp_servers`` from inside the scripted run: that is the
    only moment it exists.
    """
    installed: list[dict[str, dict[str, Any]]] = []

    def run(request: agentshim.CommandRequest) -> FakeRun:
        installed.append(installed_mcp_servers("claude", request, tmp_path))
        return scripted_turn("claude", text="ok")

    driver, _fake = _driver("claude", run)
    session = driver.create_session(
        _spec(
            tmp_path,
            mcp_servers=(MCPServerSpec(name="issues", command="python", args=("-m", "issues")),),
        )
    )

    session.run_turn(AgentTurnRequest(message="review"))

    entry = installed[0]["issues"]
    # A host session pins the interpreter that VibeSys itself is running
    # under: a login shell's bare ``python`` may not have the MCP
    # dependencies.
    assert entry["command"] == subject.sys.executable
    assert entry["args"] == ["-m", "issues"]
    assert _workspace_files(tmp_path) == {}


@pytest.mark.usefixtures("sandbox_builds")
def test_a_non_python_mcp_command_is_left_alone(tmp_path: Path) -> None:
    installed: list[dict[str, dict[str, Any]]] = []

    def run(request: agentshim.CommandRequest) -> FakeRun:
        installed.append(installed_mcp_servers("claude", request, tmp_path))
        return scripted_turn("claude", text="ok")

    driver, _fake = _driver("claude", run)
    session = driver.create_session(
        _spec(
            tmp_path,
            mcp_servers=(MCPServerSpec(name="other", command="node", args=("server.js",)),),
        )
    )

    session.run_turn(AgentTurnRequest(message="review"))

    assert installed[0]["other"]["command"] == "node"


def test_a_container_mcp_command_is_left_for_the_image_to_resolve(tmp_path: Path) -> None:
    """The container image resolves its own interpreter, so nothing is pinned."""
    server = MCPServerSpec(name="issues", command="python", args=("-m", "issues"))
    installed: list[dict[str, dict[str, Any]]] = []

    def run(request: agentshim.CommandRequest) -> FakeRun:
        installed.append(installed_mcp_servers("claude", request, tmp_path))
        return scripted_turn("claude", text="ok")

    sandbox = _FakeDockerSandbox(workspace=tmp_path)
    driver, _fake = _driver("claude", run, docker_sandboxes={"implementer": sandbox})
    session = driver.create_session(
        _spec(
            tmp_path,
            provider="claude",
            policy=AgentExecutionPolicy(containerized=True),
            mcp_servers=(server,),
        )
    )

    session.run_turn(AgentTurnRequest(message="review"))

    # installed[0] is the binary health check's own scripted run (it runs
    # through the same confined executor); the turn is the last snapshot.
    entry = installed[-1]["issues"]
    assert entry["command"] == "python"
    assert entry["args"] == ["-m", "issues"]


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


def _container_spec(tmp_path: Path, provider: str, **changes: Any) -> AgentSessionSpec:  # noqa: ANN401
    return _spec(
        tmp_path,
        provider=provider,
        policy=AgentExecutionPolicy(containerized=True),
        **changes,
    )


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_container_turn_carries_the_sandbox_environment_and_workdir(
    tmp_path: Path,
    provider: str,
) -> None:
    """The sandbox's own environment reaches the CLI, and ``-w`` carries the workdir.

    A container starts from its image's environment; there is no separate
    per-turn environment-forwarding path any more, so the session hands the
    library exactly ``sandbox.env``.
    """
    sandbox = _FakeDockerSandbox(workspace=tmp_path, extra_env={"VIBESYS_ROUND": "3"})
    driver, fake = _driver(
        provider, scripted_turn(provider, text="ok"), docker_sandboxes={"implementer": sandbox}
    )
    session = driver.create_session(_container_spec(tmp_path, provider))

    session.run_turn(AgentTurnRequest(message="Do it"))

    argv = list(fake.requests[-1].argv)
    assert argv[:3] == ["docker", "exec", "-i"]
    assert argv[argv.index("-w") + 1] == "/workspace"
    forwarded = [argv[index + 1] for index, item in enumerate(argv) if item == "-e"]
    assert "VIBESYS_ROUND=3" in forwarded
    assert sandbox.container_id in argv
    assert argv[argv.index(sandbox.container_id) + 1].endswith(
        agentshim.get_provider(provider).profile.binary
    )
    assert fake.requests[-1].env["HOME"] == sandbox.home


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_container_binary_check_gets_the_container_budget(
    tmp_path: Path,
    provider: str,
) -> None:
    """The check crosses a ``docker exec``, so it waits on the daemon too.

    A failed health check ends the run before the first turn, so the budget
    has to survive a daemon that is busy rather than dead.
    """
    sandbox = _FakeDockerSandbox(workspace=tmp_path)
    driver, fake = _driver(
        provider, scripted_turn(provider, text="ok"), docker_sandboxes={"implementer": sandbox}
    )

    driver.create_session(_container_spec(tmp_path, provider))

    check = fake.requests[0]
    assert "--help" in check.argv
    assert check.timeout == subject._CONTAINER_BINARY_CHECK_TIMEOUT_S  # noqa: SLF001


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_the_binary_check_budget_is_a_driver_option(
    tmp_path: Path,
    provider: str,
) -> None:
    sandbox = _FakeDockerSandbox(workspace=tmp_path)
    driver, fake = _driver(
        provider,
        scripted_turn(provider, text="ok"),
        docker_sandboxes={"implementer": sandbox},
        check_timeout=5,
    )

    driver.create_session(_container_spec(tmp_path, provider))

    assert fake.requests[0].timeout == 5


def _watchdog_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Record the argv of every command a container session sends to the watchdog.

    The watchdog wraps the ``docker exec`` transport rather than replacing it,
    so what it sees is the command agentshim built, before the transform. An
    empty list means the session never had one.
    """
    watched: list[tuple[str, ...]] = []
    real = docker_executor.CodexRolloutWatchdogExecutor

    class _Recording(real):  # type: ignore[misc, valid-type]
        def run(
            self,
            request: agentshim.CommandRequest,
            sink: agentshim.CommandStreamSink,
        ) -> agentshim.CommandResult:
            watched.append(tuple(request.argv))
            return super().run(request, sink)

    monkeypatch.setattr(docker_executor, "CodexRolloutWatchdogExecutor", _Recording)
    return watched


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_every_container_session_runs_its_turns_through_the_watchdog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
) -> None:
    """The watchdog wraps unconditionally: it only ever acts on a resumed Codex
    JSON run, so wrapping every provider's container session costs nothing for
    the rest and needs no per-provider branch in the driver."""
    watched = _watchdog_spy(monkeypatch)
    sandbox = _FakeDockerSandbox(workspace=tmp_path)
    driver, _fake = _driver(
        provider, scripted_turn(provider, text="ok"), docker_sandboxes={"implementer": sandbox}
    )
    session = driver.create_session(_container_spec(tmp_path, provider))

    session.run_turn(AgentTurnRequest(message="Do it"))

    assert watched, "the container session's turn did not go through the watchdog"


def test_the_watchdog_rollout_root_comes_from_the_sandbox_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No ``/root`` or ``/home/agent`` literal: it is read off the sandbox."""
    captured: dict[str, str] = {}
    real = docker_executor.CodexRolloutWatchdogExecutor

    def _spy(*args: Any, rollout_sessions_root: str, **kwargs: Any) -> Any:  # noqa: ANN401
        captured["rollout_sessions_root"] = rollout_sessions_root
        return real(*args, rollout_sessions_root=rollout_sessions_root, **kwargs)

    monkeypatch.setattr(docker_executor, "CodexRolloutWatchdogExecutor", _spy)
    sandbox = _FakeDockerSandbox(workspace=tmp_path, home="/home/somebody-else")
    driver, _fake = _driver(
        "codex", scripted_turn("codex", text="ok"), docker_sandboxes={"implementer": sandbox}
    )

    driver.create_session(_container_spec(tmp_path, "codex"))

    assert captured["rollout_sessions_root"] == "/home/somebody-else/.codex/sessions"


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_container_session_mcp_servers_are_installed_on_the_host_workspace(
    tmp_path: Path,
    provider: str,
) -> None:
    """A container turn writes its MCP config on the bind-mounted host workspace.

    The session's own ``cwd`` is the real host workspace path in every mode
    now, so agentshim's ordinary cwd-derived config directory already lands
    on the host side of the bind mount; the CLI reads it back through
    ``/workspace``. As on the host, the file exists only while the provider
    process is running, so the scripted run is the moment it can be observed.
    """
    server = MCPServerSpec(name="issues", command="python", args=("-m", "issues"))
    during: list[dict[str, str]] = []
    installed: list[dict[str, dict[str, Any]]] = []

    def run(request: agentshim.CommandRequest) -> FakeRun:
        # Every container command is scripted, the provider health check
        # included, so the turn is the last snapshot rather than the only one.
        during.append(_workspace_files(tmp_path))
        installed.append(installed_mcp_servers(provider, request, tmp_path))
        return scripted_turn(provider, text="ok")

    sandbox = _FakeDockerSandbox(workspace=tmp_path)
    driver, _fake = _driver(provider, run, docker_sandboxes={"implementer": sandbox})
    session = driver.create_session(_container_spec(tmp_path, provider, mcp_servers=(server,)))

    session.run_turn(AgentTurnRequest(message="review"))

    entry = installed[-1]["issues"]
    assert entry["command"] == "python"
    assert entry["args"] == ["-m", "issues"]

    if agentshim.get_provider(provider).profile.mcp is not agentshim.McpMechanism.CONFIG_FILE:
        # Codex passes its servers as flags, so nothing is written to the
        # workspace in either execution mode.
        assert during[-1] == {}
    else:
        assert during[-1], f"{provider} wrote no MCP config on the host workspace"
    assert _workspace_files(tmp_path) == {}, "the MCP config outlived the turn"


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_container_timeout_reports_no_docker_transport_in_its_message(
    tmp_path: Path,
    provider: str,
) -> None:
    """A timeout message must not carry the ``docker exec`` transform.

    In container mode the argv agentshim times out on is the transformed
    ``docker exec -e KEY=VALUE ...`` line, and ``AgentClient`` logs
    ``str(exc)`` on a failed round.
    """

    def run(request: agentshim.CommandRequest) -> FakeRun:
        # Every container command is scripted, the binary health check
        # included; only the turn itself is the one that hangs.
        return FakeRun() if "--help" in request.argv else FakeRun(timeout=True)

    sandbox = _FakeDockerSandbox(workspace=tmp_path, extra_env={"ANTHROPIC_AUTH_TOKEN": "secret"})
    driver, _fake = _driver(provider, run, docker_sandboxes={"implementer": sandbox})
    session = driver.create_session(_container_spec(tmp_path, provider))

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        session.run_turn(AgentTurnRequest(message="one", timeout=timedelta(seconds=5)))

    assert raised.value.cmd == [agentshim.get_provider(provider).profile.binary]
    assert "secret" not in str(raised.value)
    assert "docker" not in str(raised.value)


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
def test_a_container_native_schema_directory_is_mapped_through_agent_path(
    tmp_path: Path,
    provider: str,
) -> None:
    """The schema file lives on the host; the CLI reads it back through the mount."""
    profile = agentshim.get_provider(provider).profile
    if profile.output_schema is not agentshim.OutputSchemaStyle.FILE_PATH:
        pytest.skip(f"{provider} does not reference a schema file path")
    payload = {"analysis": "it improved", "verdict": "accept"}
    sandbox = _FakeDockerSandbox(workspace=tmp_path)
    driver, fake = _driver(
        provider,
        scripted_turn(provider, text="", structured_output=payload),
        docker_sandboxes={"implementer": sandbox},
    )
    session = driver.create_session(_container_spec(tmp_path, provider))

    session.run_turn(
        AgentTurnRequest(message="usr", instructions="sys", output_schema=JudgeResponse)
    )

    launched = " ".join(fake.requests[-1].argv) + (fake.requests[-1].stdin or "")
    assert str(tmp_path) not in launched
    assert "/workspace/.cache/vibesys/response-schemas" in launched


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

    def run(request: agentshim.CommandRequest) -> FakeRun:
        attempts.append(len(attempts))
        if not attempts[:-1]:
            return scripted_turn(provider, text="ok", session_id="session-1")
        if "session-1" in request.argv:
            return scripted_resume_failure(provider, session_id="session-1")
        return scripted_turn(provider, text="recovered", session_id="session-2")

    session, fake = _session(tmp_path, provider, run)
    session.run_turn(AgentTurnRequest(message="one"))

    result = session.run_turn(AgentTurnRequest(message="two"))

    assert result.text == "recovered"
    assert result.disposition is SessionDisposition.RESET_REQUIRED
    assert "session-1" in fake.requests[1].argv
    assert "session-1" not in fake.requests[2].argv
    # The first turn, the resumed attempt, and one retry. Retrying more than
    # once would spend an agent turn on a conversation that is already gone.
    assert len(fake.requests) == 3


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_second_failure_after_a_resume_retry_propagates(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    """The retry runs from a fresh conversation, so its failure is the agent's."""
    del sandbox_builds
    attempts: list[int] = []

    def run(_request: agentshim.CommandRequest) -> FakeRun:
        attempts.append(len(attempts))
        if not attempts[:-1]:
            return scripted_turn(provider, text="ok", session_id="session-1")
        return scripted_resume_failure(provider, session_id="session-1")

    session, fake = _session(tmp_path, provider, run)
    session.run_turn(AgentTurnRequest(message="one"))

    with pytest.raises(agentshim.CliExitError):
        session.run_turn(AgentTurnRequest(message="two"))

    assert len(fake.requests) == 3


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
    # Only the provider is named. ``str(TimeoutExpired)`` renders ``cmd``, and
    # callers log that string.
    assert raised.value.cmd == [agentshim.get_provider(provider).profile.binary]


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


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_host_policy_on_a_container_driver_is_rejected(
    tmp_path: Path,
    provider: str,
) -> None:
    sandbox = _FakeDockerSandbox(workspace=tmp_path)
    driver, _fake = _driver(
        provider, scripted_turn(provider, text="ok"), docker_sandboxes={"implementer": sandbox}
    )

    with pytest.raises(ValueError, match="container policy"):
        driver.create_session(_spec(tmp_path, provider=provider))


# ---------------------------------------------------------------------------
# Environment hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_the_launch_drops_the_inherited_pwd(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    """A stale ``PWD`` must not reach the CLI: bun-based CLIs trust it over the cwd."""
    del sandbox_builds
    driver, fake = _driver(provider, scripted_turn(provider, text="ok"))
    session = driver.create_session(
        _spec(tmp_path, provider=provider, environment=(("PWD", "/somewhere/stale"), ("GPU", "1")))
    )

    session.run_turn(AgentTurnRequest(message="Do it"))

    env = fake.requests[-1].env
    assert "PWD" not in env
    assert env["GPU"] == "1"
