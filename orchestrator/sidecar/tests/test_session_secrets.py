"""A session's credentials never rest in a file of its directory, and its logs are the owner's alone.

The tool server's token reaches Codex inside the session's own start-up request, so a log of what
crossed the provider's pipe holds it unless the sidecar removes it. These tests drive a scripted
Codex session into real files and read every file back.
"""

from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING

from agentshim.testing import CodexScript, FakeExecutor, Say
from hypothesis import given, settings
from hypothesis import strategies as st

from tests import helpers as h
from vs_sidecar.driver import REDACTED, AgentshimDriver, build_session, redact
from vs_sidecar.engine import Engine
from vs_sidecar.gen.vibesys.orchestrator.v1 import agent_session_pb2 as sc
from vs_sidecar.store import FileStore

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agentshim import RawFrame

CREDENTIAL_NAME = "VIBESYS_ORCHESTRATOR_TOKEN"
CREDENTIAL = "tok-3f9a1c7e5b2d4a60-9d8c7b6a5f4e3d2c"


def _session(directory: Path, *, secret_env: list[str]) -> tuple[Engine, CodexScript]:
    directory.mkdir(parents=True, exist_ok=True)
    script = CodexScript().turn(Say("done"))
    agent = h.scripted_agents("codex", FakeExecutor([], peers=script.peer))
    server = sc.ToolServer(
        name="vibesys",
        argv=["/bin/vibesys-host", "mcp"],
        env={CREDENTIAL_NAME: CREDENTIAL, "VIBESYS_ORCHESTRATOR_RUN": "run-1"},
        secret_env=secret_env,
    )
    request = h.open_request(tools=[server])

    def make(raw_sink: Callable[[RawFrame], None]) -> AgentshimDriver:
        return AgentshimDriver(
            lambda conversation_id: build_session(
                request, agent=agent, raw_sink=raw_sink, new_conversation_id=conversation_id
            ),
            workdir=request.working_directory,
        )

    return Engine(request.session, FileStore.at(directory), make), script


def _files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*") if path.is_file())


def test_the_token_is_in_no_file_of_a_scripted_codex_session(tmp_path: Path) -> None:
    state = tmp_path / "state"
    engine, script = _session(state, secret_env=[CREDENTIAL_NAME])
    engine.start()
    engine.send(h.start_turn("e1", "t1"))
    engine.run_next()
    engine.release()
    # The scripted peer was handed the token, so the test is about the log and not about a session
    # that never carried one.
    assert CREDENTIAL in str(script.requests("thread/start"))
    files = _files(state)
    assert {path.name for path in files} >= {"events.log", "raw.log", "effects.log"}
    leaked = [path.name for path in files if CREDENTIAL.encode() in path.read_bytes()]
    assert leaked == []
    assert REDACTED in (state / "raw.log").read_text()


def test_a_credential_the_request_does_not_name_is_not_hidden(tmp_path: Path) -> None:
    state = tmp_path / "state"
    # Redaction follows the request's own list: nothing is guessed from a name.
    engine, _ = _session(state, secret_env=[])
    engine.start()
    engine.release()
    assert CREDENTIAL in (state / "raw.log").read_text()


def test_every_log_is_private_also_one_an_earlier_build_made_open(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    older = state / "raw.log"
    older.write_text("")
    older.chmod(0o644)
    engine, _ = _session(state, secret_env=[CREDENTIAL_NAME])
    engine.start()
    engine.send(h.start_turn("e1", "t1"))
    engine.run_next()
    engine.release()
    for path in _files(state):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name


@settings(max_examples=200, deadline=None)
@given(
    values=st.lists(
        st.text(
            alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), max_codepoint=126),
            min_size=8,
            max_size=40,
        ),
        min_size=1,
        max_size=3,
    ),
    pieces=st.lists(st.text(max_size=30), min_size=1, max_size=6),
    data=st.data(),
)
def test_redaction_removes_every_credential_in_every_spelling(
    values: list[str], pieces: list[str], data: st.DataObject
) -> None:
    secrets = tuple(sorted(set(values), key=len, reverse=True))
    parts = list(pieces)
    for value in secrets:
        spelling = data.draw(st.sampled_from([value, json.dumps(value)[1:-1]]))
        parts.insert(data.draw(st.integers(0, len(parts))), spelling)
    text = "".join(parts)
    out = redact(text, secrets)
    for value in secrets:
        assert value not in out
