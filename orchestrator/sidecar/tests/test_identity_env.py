"""The provider process, and each tool server, get exactly the names the identity golden lists.

The golden is the one list of environment names the orchestrator gives an agent and the vibesys-cli
reads (the CLI's own tests read the same file). Agentshim builds a provider's environment from a
login-shell probe and never from this process's, so the carrier is the ``Agent`` built per session:
these tests read the environment of the process the scripted executor was asked to spawn.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from agentshim import StdioMcpServer
from agentshim.testing import ClaudeFramePeer, CodexScript, FakeExecutor, Say
from hypothesis import given
from hypothesis import strategies as st

from tests import helpers as h
from vs_sidecar.driver import AgentshimDriver, Identity, build_session
from vs_sidecar.gen.vibesys.orchestrator.v1 import effects_pb2 as fx
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch

GOLDEN = (
    Path(__file__).resolve().parents[3]
    / "proto"
    / "vibesys"
    / "cli"
    / "v1"
    / "conformance"
    / "identity_env.golden.json"
)
NAMES: list[dict[str, Any]] = json.loads(GOLDEN.read_text())["names"]
SESSION = "11111111-1111-4111-8111-111111111111"
INIT: dict[str, Any] = {"type": "system", "subtype": "init", "session_id": SESSION, "model": "m"}
#: What the login shell would give: other names, and stale values under the golden names that a
#: launch must replace or remove.
LOGIN_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/login",
    "EDITOR": "vi",
    "VIBESYS_CLI": "/stale/vibesys-cli",
    "XDG_RUNTIME_DIR": "/run/user/stale",
    "XDG_STATE_HOME": "/stale/state",
}

text = st.text(
    alphabet=st.characters(blacklist_categories=("Cc", "Cs"), blacklist_characters="\x00"),
    min_size=1,
    max_size=12,
)
place = st.one_of(st.just(""), text.map(lambda t: "/" + t))
launches = st.builds(
    h.launch,
    harness=st.sampled_from([orch.HARNESS_CLAUDE_CODE, orch.HARNESS_CODEX]),
    run_id=text,
    repository=text,
    task=text,
    session=st.builds(fx.SessionRef, agent_id=text, session=st.integers(1, 9)),
    cli_location=st.builds(
        orch.CliLocation, cli=place, xdg_runtime_dir=place, xdg_state_home=place, home=place
    ),
)
#: A server's own environment, trying to say it is someone else.
server_envs = st.dictionaries(
    st.sampled_from([n["name"] for n in NAMES] + ["OTHER", "TOKEN"]), text, max_size=6
)


def _value(launch: fx.LaunchSession, source: str) -> str:
    """What the golden says fills a name: a field of the launch, read independently of the sidecar."""
    if not source:
        return ""  # the orchestrator does not fill it: the agent inherits it, as `empty: keep` says
    if source == "harness":
        return {orch.HARNESS_CLAUDE_CODE: "claude-code", orch.HARNESS_CODEX: "codex"}[
            launch.harness
        ]
    if source == "session.session":
        return str(launch.session.session)
    value: Any = launch
    for part in source.split("."):
        value = getattr(value, part)
    return str(value)


def _check_environment(
    env: dict[str, str], launch: fx.LaunchSession, base: dict[str, str], *, who: str
) -> None:
    """*env* holds, for each golden name, the launch's value, and otherwise only *base*."""
    golden = {n["name"] for n in NAMES}
    for entry in NAMES:
        name, value = entry["name"], _value(launch, entry["from"])
        if value:
            assert env.get(name) == value, f"{who}: {name} is {env.get(name)!r}, want {value!r}"
        elif entry["empty"] == "unset":
            assert name not in env, f"{who}: {name}={env.get(name)!r} should be unset"
        else:
            assert env.get(name) == base.get(name), f"{who}: {name} should be left as it was"
    assert {k: v for k, v in env.items() if k not in golden} == {
        k: v for k, v in base.items() if k not in golden
    }, f"{who}: names beyond the golden's changed"


def _executor(harness: int) -> FakeExecutor:
    if harness == orch.HARNESS_CLAUDE_CODE:
        return FakeExecutor([], peers=lambda _request: ClaudeFramePeer([[INIT]]))
    return FakeExecutor([], peers=CodexScript().turn(Say("x")).peer)


def _open(
    launch: fx.LaunchSession, servers: list[StdioMcpServer]
) -> tuple[FakeExecutor, AgentshimDriver]:
    executor = _executor(launch.harness)
    provider = "claude" if launch.harness == orch.HARNESS_CLAUDE_CODE else "codex"
    agents = h.scripted_agents(provider, executor, LOGIN_ENV)
    driver = AgentshimDriver(
        lambda conversation_id: build_session(
            launch, agent=agents, mcp_servers=servers, new_conversation_id=conversation_id
        ),
        preferred_id=SESSION,
    )
    with tempfile.TemporaryDirectory() as directory:
        launch.plugin.home = str(Path(directory) / "plugin-home")
        driver.open()
    return executor, driver


@given(launch=launches, server_env=server_envs)
def test_the_provider_process_gets_exactly_the_golden_names_with_the_launchs_values(
    launch: fx.LaunchSession, server_env: dict[str, str]
) -> None:
    executor, _driver = _open(
        launch, [StdioMcpServer("vibesys", "/bin/vibesys-host", ("mcp",), server_env)]
    )
    assert executor.spawns, "the session never started its provider"
    for spawn in executor.spawns:
        _check_environment(
            dict(spawn.env),
            launch,
            LOGIN_ENV
            | {
                (
                    "CLAUDE_CONFIG_DIR"
                    if launch.harness == orch.HARNESS_CLAUDE_CODE
                    else "CODEX_HOME"
                ): launch.plugin.home
            },
            who="provider process",
        )


@given(
    launch=launches.filter(lambda launch: launch.harness == orch.HARNESS_CLAUDE_CODE),
    server_env=server_envs,
)
def test_identity_wins_over_a_servers_own_environment(
    launch: fx.LaunchSession, server_env: dict[str, str]
) -> None:
    executor, _driver = _open(
        launch, [StdioMcpServer("vibesys", "/bin/vibesys-host", ("mcp",), server_env)]
    )
    argv = list(executor.spawns[0].argv)
    config = json.loads(argv[argv.index("--mcp-config") + 1])
    env = config["mcpServers"]["vibesys"]["env"]
    # The server's own names that the golden does not list survive; the golden's are the launch's.
    own = {k: v for k, v in server_env.items() if k not in {n["name"] for n in NAMES}}
    _check_environment(env, launch, server_env, who="tool server")
    assert {k: v for k, v in env.items() if k in own} == own


def test_the_old_id_names_are_gone() -> None:
    launch = h.launch(orch.HARNESS_CLAUDE_CODE)
    executor, _driver = _open(
        launch, [StdioMcpServer("vibesys", "/bin/vibesys-host", ("mcp",), {})]
    )
    names = set(executor.spawns[0].env) | set(
        json.loads(executor.spawns[0].argv[executor.spawns[0].argv.index("--mcp-config") + 1])[
            "mcpServers"
        ]["vibesys"]["env"]
    )
    assert not names.intersection({"VIBESYS_RUN_ID", "VIBESYS_AGENT_ID"})


@pytest.mark.parametrize("chosen", ["/chosen/vibesys-cli", ""])
def test_the_sidecar_processs_own_environment_is_not_the_carrier(
    monkeypatch: pytest.MonkeyPatch, chosen: str
) -> None:
    """A name set only on the sidecar process never reaches the provider: the launch decides."""
    monkeypatch.setenv("VIBESYS_CLI", "/process/vibesys-cli")
    monkeypatch.setenv("VIBESYS_RUN", "process-run")
    launch = h.launch(orch.HARNESS_CODEX, cli_location=orch.CliLocation(cli=chosen))
    executor, _driver = _open(launch, [])
    env = executor.spawns[0].env
    assert env.get("VIBESYS_CLI") == (chosen or None)
    assert env["VIBESYS_RUN"] == launch.run_id


def test_stale_identity_is_removed_without_removing_other_contracts() -> None:
    env = Identity.of(h.launch(orch.HARNESS_CODEX)).over(
        {"VIBESYS_RUN_ID": "old-run", "VIBESYS_AGENT_ID": "old-agent", "VIBESYS_EXEC_ID": "job"}
    )
    assert "VIBESYS_RUN_ID" not in env
    assert "VIBESYS_AGENT_ID" not in env
    assert env["VIBESYS_EXEC_ID"] == "job"
