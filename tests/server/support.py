"""Test composition helpers for independently owned server components."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from server.api.service import RunApi
from server.chat.manager import ChatManager
from server.controller import RunController
from server.execution import ExecutionTracker
from server.integration import RunIntegrationAdapter
from server.journal import EventJournal
from server.read_model import RunInspector

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from server.chat.factory import ChatAgentBuilder
    from server.wire.v2 import responses_pb2
    from vs_project import Project


@dataclass(frozen=True)
class ServerParts:
    """Explicitly composed server components used by focused tests."""

    condition: threading.Condition
    journal: EventJournal
    executions: ExecutionTracker
    controller: RunController
    chat: ChatManager
    integration: RunIntegrationAdapter
    api: RunApi

    def attach(
        self,
        log_dir: Path,
        *,
        project: Project | None = None,
        run_id: str | None = None,
    ) -> None:
        """Attach the integration to durable state for one test run."""
        self.integration.attach(log_dir, project=project, run_id=run_id)

    def close(self) -> None:
        """Release subscriptions owned by the integration adapter."""
        self.integration.close()


def build_server_parts(
    log_dir: Path | None = None,
    *,
    project: Project | None = None,
    run_id: str | None = None,
    tui_defaults: Callable[[], responses_pb2.TuiDefaults] | None = None,
    chat_agent_builder: ChatAgentBuilder | None = None,
) -> ServerParts:
    """Compose real server components and optionally attach durable state."""
    condition = threading.Condition(threading.RLock())
    journal = EventJournal(condition)
    executions = ExecutionTracker(condition, journal)
    controller = RunController(condition, journal, executions)
    chat = ChatManager(condition, journal, run_status=controller.run_status)
    journal.add_listener(chat.apply_replayed_event, replay_filter=chat.replay_filter)
    if chat_agent_builder is None:
        integration = RunIntegrationAdapter(controller, executions, journal, chat)
    else:
        integration = RunIntegrationAdapter(
            controller,
            executions,
            journal,
            chat,
            chat_agent_builder=chat_agent_builder,
        )
    chat.set_fallback_answer(RunInspector(integration).answer)
    api = RunApi(
        condition,
        controller,
        executions,
        journal,
        chat,
        integration,
        tui_defaults=tui_defaults,
    )
    parts = ServerParts(
        condition=condition,
        journal=journal,
        executions=executions,
        controller=controller,
        chat=chat,
        integration=integration,
        api=api,
    )
    if log_dir is not None:
        parts.attach(log_dir, project=project, run_id=run_id)
    return parts
