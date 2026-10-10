"""The confined session launcher against the real provider CLIs.

Skipped unless ``VIBESYS_E2E_AGENTS=1`` and the provider binary is on PATH, so
an ordinary ``pytest`` run needs no credentials and makes no network calls:

```bash
VIBESYS_E2E_AGENTS=1 uv run pytest tests/e2e -q -p no:cacheprovider -s
```

What is proven here is the launcher's host path end to end, not the library's:
a real conversation resumes, a real structured turn parses, and a real
session-scoped stdio MCP server is reachable from inside host confinement.
The prompts are deliberately tiny; each case is one or two paid turns.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import agentshim
import pytest
from pydantic import BaseModel
from tests.support import run_test_command

from vs_agent.api import (
    AgentEvent,
    AgentEventKind,
    MCPServerSpec,
)
from vs_agent.contracts import (
    AgentExecutionPolicy,
    AgentSessionSpec,
    AgentTurnRequest,
)
from vs_agent.session_launch import ConfinedSessionLauncher
from vs_sandbox.api import HostResource, HostResourceAccess

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vs_agent.shim_turns import LaunchedSession

ENABLE_ENV = "VIBESYS_E2E_AGENTS"

#: (provider, model). Claude is pinned to the cheapest model that can follow
#: these instructions; Codex takes ``VIBESYS_E2E_CODEX_MODEL`` or, unset, the
#: CLI default.
PROVIDERS = (("claude", "haiku"), ("codex", os.environ.get("VIBESYS_E2E_CODEX_MODEL") or None))

#: The stdio MCP server the tool-use case exposes to the agent.
MCP_SERVER = Path(__file__).resolve().parents[1] / "support" / "mcp_add_server.py"

TURN_TIMEOUT_S = 300


#: The operands the MCP tool case asks the agent to add.
ADD_OPERANDS = ("918273", "645281")


def _is_add_call(payload: object) -> bool:
    """Whether one tool-call payload is a call of the MCP ``add`` tool.

    Claude calls the namespaced tool by name, Codex calls a generic
    ``mcp_tool_call`` and names the server and tool in its arguments. Either
    way the operands have to be in the call, so a tool whose name merely
    contains ``add`` cannot satisfy this.
    """
    if not isinstance(payload, dict):
        return False
    tool = str(payload.get("tool", ""))
    rendered = str(payload)
    named = tool.endswith("add") or "add" in str(payload.get("args", ""))
    return named and all(operand in rendered for operand in ADD_OPERANDS)


def _enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


def requires_cli(binary: str) -> pytest.MarkDecorator:
    """Skip unless e2e is enabled and *binary* is installed."""
    reason = f"set {ENABLE_ENV}=1" if not _enabled() else f"{binary} is not on PATH"
    return pytest.mark.skipif(not _enabled() or shutil.which(binary) is None, reason=reason)


pytestmark = pytest.mark.e2e

PROVIDER_PARAMS = [
    pytest.param(provider, model, marks=requires_cli(provider), id=provider)
    for provider, model in PROVIDERS
]


@pytest.fixture
def agent_env() -> Iterator[None]:
    """Run the provider CLIs as if no coding agent had launched the test.

    ``CLAUDECODE`` is exported by an outer Claude Code session and changes how
    a nested CLI behaves. agentshim reads its environment from ``bash -i`` and
    caches the answer, so the variable is removed from this process before the
    cache is refilled, and the cache is refilled again afterwards so no later
    test inherits the edited environment.
    """
    saved = {name: os.environ[name] for name in ("CLAUDECODE",) if name in os.environ}
    for name in saved:
        del os.environ[name]
    agentshim.interactive_env(refresh=True)
    try:
        yield
    finally:
        os.environ.update(saved)
        agentshim.interactive_env(refresh=True)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A git repository for the agent to run in.

    Codex refuses a directory that is not a repository unless told otherwise,
    and every provider behaves more like a real VibeSys turn inside one.
    """
    repo = tmp_path / "workspace"
    repo.mkdir()
    run_test_command(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text("e2e\n")
    return repo


class _Recorder:
    """Collect the neutral events one turn emitted."""

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def on_event(self, event: AgentEvent) -> None:
        self.events.append(event)

    def of_kind(self, kind: AgentEventKind) -> list[AgentEvent]:
        return [event for event in self.events if event.kind is kind]


class Answer(BaseModel):
    """The smallest structured response worth asking a model for."""

    answer: int


@contextmanager
def _session(
    provider: str,
    model: str | None,
    workspace: Path,
    *,
    host_resources: tuple[HostResource, ...] = (),
    spec_fields: dict[str, Any] | None = None,
) -> Iterator[LaunchedSession]:
    """Launch one session and close its launcher afterwards."""
    launcher = ConfinedSessionLauncher(provider=provider, timeout=TURN_TIMEOUT_S, log=print)
    session = launcher.launch(
        AgentSessionSpec(
            role="e2e",
            provider=provider,
            workspace=workspace,
            model=model,
            policy=AgentExecutionPolicy(
                host_resources=host_resources,
                require_enforcement=False,
            ),
            **(spec_fields or {}),
        )
    )
    try:
        yield session
    finally:
        launcher.close()


def _report(label: str, **values: object) -> None:
    """Print what the real CLI answered, so ``-s`` runs are self-documenting."""
    sys.stdout.write(
        f"[e2e {label}] " + " | ".join(f"{key}={value!r}" for key, value in values.items()) + "\n"
    )


@pytest.mark.parametrize(("provider", "model"), PROVIDER_PARAMS)
@pytest.mark.usefixtures("agent_env")
def test_one_turn_answers_and_reports_a_conversation_and_usage(
    provider: str,
    model: str | None,
    workspace: Path,
) -> None:
    with _session(provider, model, workspace) as session:
        result = session.run_turn(
            AgentTurnRequest(
                message="Reply with exactly one word: pong",
                instructions="Answer directly. Do not use any tools.",
            )
        )

        _report(
            f"{provider} single-turn",
            text=result.text,
            session=result.provider_session_id,
            usage=result.usage,
        )
        assert "pong" in result.text.lower()
        assert result.provider_session_id
        assert result.usage.input_tokens is not None
        assert result.usage.input_tokens > 0


@pytest.mark.parametrize(("provider", "model"), PROVIDER_PARAMS)
@pytest.mark.usefixtures("agent_env")
def test_a_second_turn_resumes_the_same_conversation(
    provider: str,
    model: str | None,
    workspace: Path,
) -> None:
    with _session(provider, model, workspace) as session:
        first = session.run_turn(
            AgentTurnRequest(
                message="Remember the word 'juniper'. Reply with exactly: ok",
                instructions="Answer directly. Do not use any tools.",
            )
        )
        second = session.run_turn(
            AgentTurnRequest(
                message="What word did I ask you to remember? Reply with just that word.",
                instructions="Answer directly. Do not use any tools.",
            )
        )

        _report(
            f"{provider} resume",
            first_text=first.text,
            first_session=first.provider_session_id,
            second_text=second.text,
            second_session=second.provider_session_id,
            usage=second.usage,
        )
        assert "juniper" in second.text.lower()
        # The conversation is what has to survive; the same id on both turns is
        # the proof the second one resumed rather than replayed.
        assert not first.restarted
        assert second.provider_session_id == first.provider_session_id


@pytest.mark.parametrize(("provider", "model"), PROVIDER_PARAMS)
@pytest.mark.usefixtures("agent_env")
def test_a_structured_turn_parses_into_the_response_model(
    provider: str,
    model: str | None,
    workspace: Path,
) -> None:
    with _session(provider, model, workspace) as session:
        result = session.run_turn(
            AgentTurnRequest(
                message="What is 6 times 7?",
                instructions="Answer directly. Do not use any tools.",
                output_schema=Answer,
            )
        )

        _report(f"{provider} structured", text=result.text, usage=result.usage)
        assert Answer.model_validate_json(result.text).answer == 42


@pytest.mark.parametrize(("provider", "model"), PROVIDER_PARAMS)
@pytest.mark.usefixtures("agent_env")
def test_a_session_mcp_server_is_reachable_from_inside_confinement(
    provider: str,
    model: str | None,
    workspace: Path,
) -> None:
    """The agent must reach a stdio MCP server VibeSys started for the session.

    The sum is one the model has no reason to know, so an answer that matches
    is evidence the tool ran rather than evidence of arithmetic.
    """
    servers = (
        MCPServerSpec(
            name="calc",
            command="python",
            args=(str(MCP_SERVER),),
        ),
    )
    grants = (HostResource(MCP_SERVER.parent, HostResourceAccess.READ_ONLY, "e2e MCP server"),)
    with _session(
        provider, model, workspace, host_resources=grants, spec_fields={"mcp_servers": servers}
    ) as session:
        recorder = _Recorder()
        result = session.run_turn(
            AgentTurnRequest(
                message=(
                    f"Use the MCP tool named 'add' to compute "
                    f"{ADD_OPERANDS[0]} + {ADD_OPERANDS[1]}, "
                    "then reply with only the number it returned."
                ),
                instructions="You must call the tool. Do not compute the sum yourself.",
            ),
            recorder,
        )

        # Claude names the namespaced tool directly (``mcp__calc__add``);
        # Codex reports one generic ``mcp_tool_call`` item and names the server
        # and tool in its arguments. So the tool name is matched where each
        # provider puts it, and the operands are required alongside it: a call
        # to some unrelated tool whose payload merely contains the letters
        # "add" is not evidence that the MCP server was reached.
        calls = [event.payload for event in recorder.of_kind(AgentEventKind.TOOL_CALL)]
        _report(f"{provider} mcp", text=result.text, tools=calls, usage=result.usage)
        assert any(_is_add_call(call) for call in calls), calls
        assert "1563554" in result.text.replace(",", "")


def _seed_operator_mcp_server(
    provider: str, workspace: Path, root: Path
) -> tuple[tuple[str, str], ...]:
    """Configure an MCP server the way an operator would, outside the run.

    Claude Code reads a project ``.mcp.json``; Codex reads ``config.toml`` in
    its state root, which is relocated here (with the real auth files copied
    in) so the test never touches the real one. Returns the environment that
    points the CLI at it.
    """
    command, args = sys.executable, str(MCP_SERVER)
    if provider == "claude":
        config = {"mcpServers": {"operator-calc": {"command": command, "args": [args]}}}
        (workspace / ".mcp.json").write_text(json.dumps(config))
        return ()
    profile = agentshim.get_provider(provider).profile
    assert profile.state_root_env is not None
    state_dir = profile.state_dirs[0]
    home = root / "operator-state"
    real_home = Path.home()
    for auth_file in profile.auth_files:
        source = real_home / auth_file
        if auth_file.startswith(f"{state_dir}/") and source.is_file():
            target = home / auth_file.removeprefix(f"{state_dir}/")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        f"[mcp_servers.operator_calc]\ncommand = {json.dumps(command)}\nargs = {json.dumps([args])}\n"
    )
    return ((profile.state_root_env, str(home)),)


@pytest.mark.parametrize(("provider", "model"), PROVIDER_PARAMS)
@pytest.mark.usefixtures("agent_env")
def test_a_session_reaches_the_runs_mcp_server_and_not_the_operators(
    provider: str,
    model: str | None,
    workspace: Path,
    tmp_path: Path,
) -> None:
    """Both servers offer ``add``; only the one the run configured may be called.

    The operator's copy is seeded where the CLI loads MCP servers by default.
    The run's server is called, and the operator's is not even offered.
    """
    environment = _seed_operator_mcp_server(provider, workspace, tmp_path)
    servers = (MCPServerSpec(name="calc", command="python", args=(str(MCP_SERVER),)),)
    grants = (HostResource(MCP_SERVER.parent, HostResourceAccess.READ_ONLY, "e2e MCP server"),)
    with _session(
        provider,
        model,
        workspace,
        host_resources=grants,
        spec_fields={"mcp_servers": servers, "environment": environment},
    ) as session:
        recorder = _Recorder()
        result = session.run_turn(
            AgentTurnRequest(
                message=(
                    f"Call the MCP tool 'add' with {ADD_OPERANDS[0]} and {ADD_OPERANDS[1]} on "
                    "every MCP server that offers it, one call per server. "
                    "Then reply with only the number."
                ),
                instructions="You must call the tool. Do not compute the sum yourself.",
            ),
            recorder,
        )
        calls = [event.payload for event in recorder.of_kind(AgentEventKind.TOOL_CALL)]
        _report(f"{provider} mcp scope", text=result.text, tools=calls)
        assert any(_is_add_call(call) for call in calls), calls
        assert not any("operator" in str(call) for call in calls), calls
