"""Tests for the AgentShim driver against the library's public API.

Every turn is scripted with :func:`agentshim.testing.scripted_turn`, which
emits the provider's real stream format, so nothing here hand-writes provider
JSON or reaches into ``agentshim.core``/``agentshim.providers``. The assertions
are about VibeSys policy: what argv the provider was launched with, which
events the observer saw, how usage maps onto the neutral contract, and when a
conversation is retired.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest


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
from agentshim.testing import FakeExecutor, FakeRun, TokenUsage, scripted_turn

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

SCRIPTED_PROVIDERS = ("claude",)
"""Providers whose stream format the installed library can script.

Every provider VibeSys ships behaves the same way through the driver, so these
cases are parametrized rather than written per provider.
"""

requires_codex = pytest.mark.skipif(
    "codex" not in agentshim.provider_names(),
    reason="codex provider not yet in the library snapshot",
)


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
                cache_write_input_tokens=200,
            ),
        ),
    )

    result = session.run_turn(AgentTurnRequest(message="Do it", instructions="Rules"))

    assert result.text == "done"
    assert result.provider_session_id == "session-1"
    assert result.disposition is SessionDisposition.REUSABLE
    # Cached tokens are part of the input total on every provider, and the two
    # cache fields keep their separate meanings.
    assert result.usage.input_tokens == 1200
    assert result.usage.output_tokens == 30
    assert result.usage.cache_read_input_tokens == 1000
    assert result.usage.cache_creation_input_tokens == 200
    assert result.usage.duration_ms is not None


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
    assert ("high" in fake.requests[-1].argv) is profile.supports_reasoning_effort


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
    assert call.payload["tool"] == "shell"
    assert call.payload["args"] == {"command": "cargo test"}
    result = observer.of_kind(AgentEventKind.TOOL_RESULT)[0]
    assert result.text == "ok"
    duration = result.payload["duration"]
    assert result.payload["result_payload"] == CommandResultPayload(
        stdout="ok",
        stderr="",
        exit_code=None,
        duration=duration if isinstance(duration, float) else None,
    )
    # The flat fields remain for consumers that predate the typed payload.
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
# Structured output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", SCRIPTED_PROVIDERS)
def test_a_native_schema_replaces_the_prompt_contract(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
    provider: str,
) -> None:
    del sandbox_builds
    payload = {"analysis": "it improved", "verdict": "accept"}
    session, fake = _session(
        tmp_path,
        provider,
        scripted_turn(provider, text="", structured_output=payload),
    )

    result = session.run_turn(
        AgentTurnRequest(message="usr", instructions="sys", output_schema=JudgeResponse)
    )

    assert "Schema for JudgeResponse" not in (fake.requests[-1].stdin or "")
    # The caller parses the answer back into its response model, so the
    # schema-conformant payload is what the turn reports as its text.
    assert json.loads(result.text) == payload


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


@requires_codex
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
            return FakeRun(returncode=1, stderr=["no conversation found\n"])
        return scripted_turn(provider, text="recovered", session_id="session-2")

    session, fake = _session(tmp_path, provider, run)
    session.run_turn(AgentTurnRequest(message="one"))

    result = session.run_turn(AgentTurnRequest(message="two"))

    assert result.text == "recovered"
    assert result.disposition is SessionDisposition.RESET_REQUIRED
    assert "session-1" in fake.requests[1].argv
    assert "session-1" not in fake.requests[2].argv


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


@requires_codex
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


@requires_codex
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


@requires_codex
def test_a_missing_codex_rollout_restarts_the_conversation(
    sandbox_builds: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    """Codex names the cause: the rollout the thread ID points at is gone."""
    del sandbox_builds

    attempts: list[int] = []

    def run(request: agentshim.CommandRequest) -> FakeRun:
        attempts.append(len(attempts))
        if not attempts[:-1]:
            return scripted_turn("codex", text="ok", session_id="thread-1")
        if "thread-1" in request.argv:
            return FakeRun(
                returncode=1,
                stderr=["thread/resume failed: no rollout found for thread id thread-1\n"],
            )
        return scripted_turn("codex", text="recovered", session_id="thread-2")

    session, _fake = _session(tmp_path, "codex", run)
    session.run_turn(AgentTurnRequest(message="one"))

    result = session.run_turn(AgentTurnRequest(message="two"))

    assert result.text == "recovered"
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

    with pytest.raises(ValueError, match="cannot create"):
        driver.create_session(_spec(tmp_path, provider="gemini"))


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
