"""The AgentShim driver against the real provider CLIs.

Skipped unless ``VIBESYS_E2E_AGENTS=1`` and the provider binary is on PATH, so
an ordinary ``pytest`` run needs no credentials and makes no network calls:

```bash
VIBESYS_E2E_AGENTS=1 uv run pytest tests/e2e -q -p no:cacheprovider -s
```

What is proven here is the driver's host path end to end, not the library's:
a real conversation resumes, a real structured turn parses, and a real
session-scoped stdio MCP server is reachable from inside host confinement.
The prompts are deliberately tiny; each case is one or two paid turns.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import agentshim
import pytest
from pydantic import BaseModel

from vibesys.agents.contracts import (
    AgentEvent,
    AgentEventKind,
    AgentExecutionPolicy,
    AgentSessionSpec,
    AgentTurnRequest,
    MCPServerSpec,
    SessionDisposition,
)
from vibesys.agents.docker_executor import DockerCommandExecutor
from vibesys.agents.drivers import agentshim as agentshim_driver
from vibesys.agents.drivers.agentshim import AgentShimDriver
from vs_sandbox import HostResource, HostResourceAccess

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vibesys.agents.contracts import AgentSession

ENABLE_ENV = "VIBESYS_E2E_AGENTS"

#: (provider, model). Codex takes the CLI default; Claude is pinned to the
#: cheapest model that can follow these instructions.
PROVIDERS = (("claude", "haiku"), ("codex", None))

#: The stdio MCP server the tool-use case exposes to the agent.
MCP_SERVER = Path(__file__).resolve().parents[1] / "support" / "mcp_add_server.py"

TURN_TIMEOUT_S = 300

#: How many turns the resume case runs on one session.
TURNS_HERE = 2

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
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # noqa: S607
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
    mcp_servers: tuple[MCPServerSpec, ...] = (),
    host_resources: tuple[HostResource, ...] = (),
) -> Iterator[AgentSession]:
    """Open one driver session and close its driver afterwards."""
    driver = AgentShimDriver(provider=provider, timeout=TURN_TIMEOUT_S, log=print)
    session = driver.create_session(
        AgentSessionSpec(
            role="e2e",
            provider=provider,
            workspace=workspace,
            model=model,
            policy=AgentExecutionPolicy(
                host_resources=host_resources,
                require_enforcement=False,
            ),
            mcp_servers=mcp_servers,
        )
    )
    try:
        yield session
    finally:
        driver.close()


def _report(label: str, **values: object) -> None:
    """Print what the real CLI answered, so ``-s`` runs are self-documenting."""
    print(  # noqa: T201
        f"[e2e {label}] " + " | ".join(f"{key}={value!r}" for key, value in values.items())
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
        assert first.disposition is SessionDisposition.REUSABLE
        assert second.provider_session_id == first.provider_session_id
        # VibeSys retires a Codex thread once its turn budget is spent. Whether
        # this turn is the one that spends it is read from the budget rather
        # than assumed, so raising the budget changes the expectation instead
        # of breaking the test. The answer above still stands either way; only
        # the next prompt would start cold.
        codex_turn_budget = agentshim_driver._MAX_CODEX_SESSION_TURNS  # noqa: SLF001
        budget_spent = provider == "codex" and codex_turn_budget <= TURNS_HERE
        expected = (
            SessionDisposition.RESET_REQUIRED if budget_spent else SessionDisposition.REUSABLE
        )
        assert second.disposition is expected


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
        provider, model, workspace, mcp_servers=servers, host_resources=grants
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


#: How long the "upstream still broken" probe allows a resumed turn before
#: concluding it did not exit on its own. The prompts are tiny, so this only
#: needs to be generous enough to absorb normal CLI startup and model latency.
_UPSTREAM_PROBE_TIMEOUT_S = 90.0

#: A running container with a working, authenticated ``codex`` install, for
#: the container variant of the same probe. Unset by default: this is a
#: separate opt-in from ``VIBESYS_E2E_AGENTS`` because it also requires a
#: container prepared outside this test run.
_CODEX_CONTAINER_ENV = "VIBESYS_E2E_CODEX_CONTAINER"


def _codex_container_id() -> str | None:
    return os.environ.get(_CODEX_CONTAINER_ENV)


@requires_cli("codex")
@pytest.mark.usefixtures("agent_env")
def test_upstream_codex_resume_exit_bug_probe_on_the_host(workspace: Path) -> None:
    """Whether the resumed-turn-never-exits bug is container-specific.

    ``CodexRolloutWatchdogExecutor`` in ``vibesys.agents.docker_executor``
    exists because a resumed ``codex exec resume <id> --json`` finishes its
    turn but never exits *inside a container*. This drives the identical
    resumed-turn shape through plain ``agentshim.CliAgent`` on the host --
    no container, no ``DockerCommandExecutor``, no watchdog -- to check
    whether the same failure to exit also shows up there.

    A PASS here is expected: the bug as documented is container-specific, so
    the host path should resume and exit normally on its own. A FAILURE
    (the second turn hangs until ``timeout`` kills it, raising
    ``agentshim.CliTimeoutError``) means the bug reproduces on the host too,
    which the watchdog's docstring does not currently account for.
    """
    agent = agentshim.CliAgent("codex")
    first = agent.start_session(cwd=str(workspace)).turn(
        agentshim.TurnRequest(
            prompt="Remember the word 'juniper'. Reply with exactly: ok",
            timeout=_UPSTREAM_PROBE_TIMEOUT_S,
        )
    )
    resumed = agent.start_session(cwd=str(workspace), session_id=first.session_id)

    second = resumed.turn(
        agentshim.TurnRequest(
            prompt="What word did I ask you to remember? Reply with just that word.",
            timeout=_UPSTREAM_PROBE_TIMEOUT_S,
        )
    )

    _report("codex host upstream probe", text=second.text, duration_ms=second.duration_ms)
    assert "juniper" in second.text.lower()


@pytest.mark.skipif(
    not _enabled() or not _codex_container_id(),
    reason=f"set {ENABLE_ENV}=1 and {_CODEX_CONTAINER_ENV} to a running container with codex",
)
@pytest.mark.usefixtures("agent_env")
def test_watchdog_retire_signal_resumed_codex_turn_exits_on_its_own_in_a_container() -> None:
    """The real retire signal for ``CodexRolloutWatchdogExecutor``.

    The watchdog (``vibesys.agents.docker_executor.CodexRolloutWatchdogExecutor``)
    exists only because this does not happen today: a resumed
    ``codex exec --json`` run inside a container finishes its turn but never
    exits, so the ``docker exec`` fronting it blocks until the turn budget is
    spent. This drives that exact shape -- a resumed turn inside a real
    container, through the plain ``DockerCommandExecutor`` transport, with no
    watchdog in front of it.

    A PASS means the upstream bug is fixed and the watchdog can be deleted.
    It is expected to FAIL (the second turn hangs until ``timeout`` kills it)
    for as long as the bug is present.
    """
    container_id = _codex_container_id()
    assert container_id
    executor = DockerCommandExecutor(lambda: container_id)
    agent = agentshim.CliAgent("codex", executor=executor)
    first = agent.start_session().turn(
        agentshim.TurnRequest(
            prompt="Remember the word 'juniper'. Reply with exactly: ok",
            timeout=TURN_TIMEOUT_S,
        )
    )
    resumed = agent.start_session(session_id=first.session_id)

    second = resumed.turn(
        agentshim.TurnRequest(
            prompt="What word did I ask you to remember? Reply with just that word.",
            timeout=TURN_TIMEOUT_S,
        )
    )

    _report("codex container retire-signal", text=second.text, duration_ms=second.duration_ms)
    assert "juniper" in second.text.lower()
