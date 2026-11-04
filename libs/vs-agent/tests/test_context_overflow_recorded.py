"""agentshim's context facts reach VibeSys through the real shim translation.

The provider output is recorded CLI stdout, copied from agentshim 0.23.0's
``tests/fixtures`` (``fixtures/agentshim_recorded``): Claude Code 2.1.304 on
``claude-haiku-4-5`` for a two-call turn and for a prompt the API refused with a
400, and codex-cli 0.160.0 on ``gpt-6-luna`` for ``codex exec`` relaying the
Responses API's ``context_length_exceeded``. The Claude ``init`` frame keeps
only its session id, model and CLI version, and the ``result`` frame drops
``subagent_stats``; every other frame is verbatim.

agentshim classifies the overflow as ``FailureKind.CONTEXT_OVERFLOW`` and reads
the context the last model call left as ``TurnResult.context_tokens``; the
launcher maps the first to ``AgentContextOverflowError`` and keeps the second on
the session, where the client reads it before the next resume.
"""

from __future__ import annotations

import json
from pathlib import Path

import agentshim
import pytest
from agentshim.testing import FakeClock, FakeExecutor, FakeRun

from vs_agent.api import (
    AgentClient,
    AgentContextOverflowError,
    AgentExecutionPolicy,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnRequest,
    SessionScope,
)

# test-isolation: the session's context reading is the launcher's, which the facade deliberately hides
from vs_agent.session_launch import ConfinedSessionLauncher
from vs_sandbox.api import SANDBOX_DISABLE_ENV
from vs_sim.api.testing import ManualClock, SimThreads

RECORDED = Path(__file__).parent / "fixtures" / "agentshim_recorded"
_RECORDED_CONTEXT = 8 + 21_996 + 238 + 3
"""The last model call of ``claude_two_model_calls``: uncached input, cache reads, cache
writes and output, read off its last assistant frame."""
_KEY = AgentSessionKey(SessionScope.MEMBER, "implementer:worker")


@pytest.fixture(scope="module")
def home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway operator HOME: the launcher prepares provider state under it."""
    return tmp_path_factory.mktemp("operator-home")


_FAILED = frozenset({"claude_prompt_too_long.jsonl", "codex_context_length_exceeded.jsonl"})
"""The recordings whose CLI exited 1."""


def _recorded(name: str, *, session_id: str | None = None) -> FakeRun:
    """One recorded run; *session_id* renames its conversation, as a new one would be named."""
    lines = (RECORDED / name).read_text(encoding="utf-8").splitlines(keepends=True)
    if session_id is not None:
        lines = [_with_session(line, session_id) for line in lines]
    return FakeRun(stdout=lines, returncode=1 if name in _FAILED else 0)


def _with_session(line: str, session_id: str) -> str:
    frame = json.loads(line)
    if "session_id" in frame:
        frame["session_id"] = session_id
    return json.dumps(frame) + "\n"


def _launcher(provider: str, executor: FakeExecutor, home: Path) -> ConfinedSessionLauncher:
    return ConfinedSessionLauncher(
        provider=provider,
        executor_factory=lambda: executor,
        launcher_env=lambda: {
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            SANDBOX_DISABLE_ENV: "off",
        },
        clock=FakeClock(),
        threads=SimThreads(),
    )


def _spec(root: Path, provider: str, model: str | None = None) -> AgentSessionSpec:
    return AgentSessionSpec(
        role="implementer",
        provider=provider,
        model=model,
        workspace=root,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )


@pytest.mark.parametrize(
    ("provider", "name"),
    [
        ("claude", "claude_prompt_too_long.jsonl"),
        ("codex", "codex_context_length_exceeded.jsonl"),
    ],
)
def test_a_recorded_overflow_is_an_agent_context_overflow_error(
    provider: str, name: str, home: Path, tmp_path: Path
) -> None:
    launcher = _launcher(provider, FakeExecutor([_recorded(name)]), home)
    session = launcher.launch(_spec(tmp_path, provider))

    with pytest.raises(AgentContextOverflowError) as caught:
        session.run_turn(AgentTurnRequest(message="go"))

    assert caught.value.provider == provider
    assert caught.value.detail
    assert isinstance(caught.value.__cause__, agentshim.TurnFailedError)
    assert caught.value.__cause__.kind is agentshim.FailureKind.CONTEXT_OVERFLOW


def test_a_recorded_turn_leaves_its_last_model_calls_context_on_the_session(
    home: Path, tmp_path: Path
) -> None:
    launcher = _launcher("claude", FakeExecutor([_recorded("claude_two_model_calls.jsonl")]), home)
    session = launcher.launch(_spec(tmp_path, "claude"))
    assert session.context_tokens is None

    session.run_turn(AgentTurnRequest(message="go"))

    assert session.context_tokens == _RECORDED_CONTEXT


def test_a_resume_the_provider_refuses_for_overflow_runs_once_more_in_a_fresh_conversation(
    home: Path, tmp_path: Path
) -> None:
    """The whole path: recorded CLI output, agentshim, the launcher and the client's retry."""
    executor = FakeExecutor(
        [
            _recorded("claude_two_model_calls.jsonl", session_id="conversation-1"),
            _recorded("claude_prompt_too_long.jsonl", session_id="conversation-1"),
            _recorded("claude_two_model_calls.jsonl", session_id="conversation-2"),
        ]
    )
    threads = SimThreads()
    client = AgentClient(
        ConfinedSessionLauncher(
            provider="claude",
            executor_factory=lambda: executor,
            launcher_env=lambda: {
                "PATH": "/usr/bin:/bin",
                "HOME": str(home),
                SANDBOX_DISABLE_ENV: "off",
            },
            clock=FakeClock(),
            threads=threads,
        ),
        provider="claude",
        run_log_file=None,
        clock=ManualClock(),
        threads=threads,
    )
    spec = _spec(tmp_path, "claude", model="claude-haiku-4-5")
    try:

        def turns() -> str | None:
            client.run(
                session_spec=spec,
                turn=AgentTurnRequest(message="work", instructions="Implement it."),
                session_key=_KEY,
            )
            renewed = client.run(
                session_spec=spec,
                turn=AgentTurnRequest(
                    message="next attempt",
                    instructions="Implement it.",
                    expected_provider_session_id="conversation-1",
                    renewable=True,
                ),
                session_key=_KEY,
            )
            return renewed.provider_session_id

        assert threads.run(turns) == "conversation-2"
    finally:
        client.close()

    argvs = [list(request.argv) for request in executor.requests]
    assert len(argvs) == 3
    assert "--resume" not in argvs[0]
    assert argvs[1][argvs[1].index("--resume") + 1] == "conversation-1"
    assert "--resume" not in argvs[2]
