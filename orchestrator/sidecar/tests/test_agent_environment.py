"""The provider process, and each tool server, get what the request says and nothing it does not.

The holder does not interpret the names: the host builds the environment from messages, and its own
tests hold that to their definitions. Agentshim builds a provider's environment from a login-shell probe and never
from this process's, so the carrier is the ``Agent`` built per session: these tests read the
environment of the process the scripted executor was asked to spawn.
"""

from __future__ import annotations

import json
import string
import tempfile
from pathlib import Path
from typing import Any

import pytest
from agentshim.testing import ClaudeFramePeer, CodexScript, FakeExecutor, Say
from hypothesis import given
from hypothesis import strategies as st

from tests import helpers as h
from vs_sidecar.driver import AgentshimDriver, build_session, over
from vs_sidecar.gen.vibesys.orchestrator.v1 import agent_session_pb2 as sc
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch

SESSION = "11111111-1111-4111-8111-111111111111"
INIT: dict[str, Any] = {"type": "system", "subtype": "init", "session_id": SESSION, "model": "m"}
#: What the login shell would give: other names, and values a request may replace or remove.
LOGIN_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/login",
    "EDITOR": "vi",
    "A_NAME": "from the login shell",
    "ANOTHER": "from the login shell",
}

names = st.sampled_from(["A_NAME", "ANOTHER", "NEW_NAME", "HOME", "PATH_EXTRA"])
values = st.text(
    alphabet=st.characters(blacklist_categories=("Cc", "Cs"), blacklist_characters="\x00"),
    min_size=1,
    max_size=12,
)
environments = st.tuples(st.dictionaries(names, values), st.lists(names, unique=True)).map(
    lambda pair: sc.AgentEnvironment(
        set=pair[0], unset=[name for name in pair[1] if name not in pair[0]]
    )
)


def _executor(harness: int) -> FakeExecutor:
    if harness == orch.HARNESS_CLAUDE_CODE:
        return FakeExecutor([], peers=lambda _request: ClaudeFramePeer([[INIT]]))
    return FakeExecutor([], peers=CodexScript().turn(Say("x")).peer)


def _open(request: sc.OpenSessionRequest) -> FakeExecutor:
    executor = _executor(request.harness)
    provider = "claude" if request.harness == orch.HARNESS_CLAUDE_CODE else "codex"
    agents = h.scripted_agents(provider, executor, LOGIN_ENV)
    driver = AgentshimDriver(
        lambda conversation_id: build_session(
            request, agent=agents, new_conversation_id=conversation_id
        ),
        preferred_id=SESSION,
    )
    with tempfile.TemporaryDirectory() as directory:
        request.agent_data_directory = str(Path(directory) / "data")
        driver.open()
    return executor


@given(
    environment=environments,
    harness=st.sampled_from([orch.HARNESS_CLAUDE_CODE, orch.HARNESS_CODEX]),
)
def test_the_provider_process_gets_the_environment_laid_over_the_login_one(
    environment: sc.AgentEnvironment, harness: int
) -> None:
    request = h.open_request(harness, environment=environment)
    executor = _open(request)
    assert executor.spawns, "the session never started its provider"
    wanted = over(environment, LOGIN_ENV) | {
        ("CLAUDE_CONFIG_DIR" if harness == orch.HARNESS_CLAUDE_CODE else "CODEX_HOME"): (
            request.agent_data_directory
        )
    }
    for spawn in executor.spawns:
        assert dict(spawn.env) == wanted


@given(environment=environments)
def test_laying_over_sets_and_unsets_exactly_what_it_says(environment: sc.AgentEnvironment) -> None:
    merged = over(environment, LOGIN_ENV)
    for name, value in environment.set.items():
        assert merged[name] == value
    for name in environment.unset:
        assert name not in merged
    named = {*environment.set, *environment.unset}
    assert {k: v for k, v in merged.items() if k not in named} == {
        k: v for k, v in LOGIN_ENV.items() if k not in named
    }


@given(tool_env=st.dictionaries(st.sampled_from(["TOKEN", "A_NAME", "OTHER"]), values))
def test_a_tool_server_gets_exactly_its_own_environment(tool_env: dict[str, str]) -> None:
    request = h.open_request(
        orch.HARNESS_CLAUDE_CODE,
        environment=sc.AgentEnvironment(set={"A_NAME": "the agent's"}),
        tools=[sc.ToolServer(name="vibesys", argv=["/bin/vibesys-host", "mcp"], env=tool_env)],
    )
    argv = _open(request).spawns[0].inlined_argv()
    config = json.loads(argv[argv.index("--mcp-config") + 1])
    assert config["mcpServers"]["vibesys"].get("env", {}) == tool_env


@given(token=st.text(alphabet=string.ascii_letters + string.digits, min_size=8, max_size=40))
def test_a_tool_servers_token_is_on_no_command_line(token: str) -> None:
    """Every local user can read a process's command line, and a tool server's token opens a session."""
    server = sc.ToolServer(name="vibesys", argv=["/bin/vibesys-host", "mcp"], env={"TOKEN": token})
    request = h.open_request(orch.HARNESS_CLAUDE_CODE, tools=[server])
    spawn = _open(request).spawns[0]
    assert not any(token in argument for argument in spawn.argv)
    assert "--mcp-config" in spawn.argv
    assert not any(token in value for value in spawn.env.values())


@pytest.mark.parametrize("chosen", ["/chosen/thing", None])
def test_the_processs_own_environment_is_not_the_carrier(
    monkeypatch: pytest.MonkeyPatch, chosen: str | None
) -> None:
    """A name set only on this process never reaches the provider: the request decides."""
    monkeypatch.setenv("A_NAME", "/process/thing")
    environment = (
        sc.AgentEnvironment(set={"A_NAME": chosen})
        if chosen
        else sc.AgentEnvironment(unset=["A_NAME"])
    )
    executor = _open(h.open_request(orch.HARNESS_CODEX, environment=environment))
    assert executor.spawns[0].env.get("A_NAME") == chosen
