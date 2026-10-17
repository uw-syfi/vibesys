"""The agent client of an agent-less run: scripted replies stand in for the model.

An agent-less run drives the real backend (workspaces, builds, trusted evaluations, state) with
agents that are code. This client is :class:`~vs_agent.fake_client.FakeAgentClient` configured the
way such a run needs it: it reports the ``scripted`` backend and the capabilities a CLI agent
reports, and each agent ``kind`` is answered by a *responder* the application supplies. A responder
is called with the turn (:class:`~vs_agent.fake_client.FakeInvocation`: prompt, workspace, reply
type, session key) and returns the reply, or raises what a failing turn raises
(:class:`~vs_agent.contracts.AgentOutputSchemaError` for a reply the schema refuses, any other
error for a turn that was lost). It may edit the turn's workspace, as an agent does.

This module knows no application reply schema: which replies a role gets, and what a turn does to
its workspace, is the application's script (for ``dynamic``, ``vibesys.orchestration.dynamic.scripted``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_agent.contracts import AgentCapabilities
from vs_agent.fake_client import FakeAgentClient, FakeInvocation
from vs_agent.sink import NULL_AGENT_EVENT_SINK
from vs_agent.skills import NULL_SKILL_SELECTION

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from vs_agent.fake_client import ResponseSource, TextSource
    from vs_agent.session_store import SessionStore
    from vs_agent.sink import AgentEventSink
    from vs_agent.skills import SkillSelection
    from vs_sim.api import Threads

type ScriptedTurn = FakeInvocation
"""One turn as a responder sees it: the prompt, the workspace, the reply type, the session key."""

SCRIPTED_BACKEND = "scripted"
"""The backend name a scripted client reports, and the value of ``AgentBackend.SCRIPTED``."""

SCRIPTED_CAPABILITIES = AgentCapabilities(session_reuse=True, provider_session_resume=True)
"""What a scripted agent offers: conversations that continue and resume, as a CLI agent's do."""


@dataclass(frozen=True, slots=True)
class ScriptedClientSetup:
    """What one scripted client answers with, and the run services it is given."""

    responders: Mapping[str, ResponseSource]
    """The responder of each agent kind; a kind with none fails its turn as a refused reply."""
    chat: TextSource
    """The answer to a conversational turn (the auxiliary chat agents)."""
    provider: str | None = None
    model: str | None = None
    event_sink: AgentEventSink = NULL_AGENT_EVENT_SINK
    session_store: SessionStore | None = None
    skill_selection: SkillSelection = NULL_SKILL_SELECTION
    log_dir: Path | None = None


class ScriptedAgentClient(FakeAgentClient):
    """A client whose turns are answered by the application's responders, one per agent kind."""

    def __init__(self, setup: ScriptedClientSetup, *, threads: Threads) -> None:
        """Answer each kind in ``setup.responders`` with its responder, and chat with its text."""
        super().__init__(
            backend_name=SCRIPTED_BACKEND,
            provider=setup.provider,
            model=setup.model,
            capabilities=SCRIPTED_CAPABILITIES,
            event_sink=setup.event_sink,
            session_store=setup.session_store,
            skill_selection=setup.skill_selection,
            log_dir=setup.log_dir,
            threads=threads,
        )
        for kind, responder in setup.responders.items():
            self.set_response(kind, responder)
        self.set_text(None, setup.chat)
