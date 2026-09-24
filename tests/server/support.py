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
from vibesys.run.event_journal import EventJournal as CoreEventJournal
from vibesys.run.run_control import RunControlChannel

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from server.chat.factory import ChatAgentBuilder
    from server.settings import InteractiveSetupDefaults
    from vibesys.api import RunView
    from vs_project.api import Project


class _ControlBridge:
    """Adapt a `RunControlChannel` to the `vibesys.api.RunControl` shape.

    Mirrors what `vibesys.api.session._LocalRunSession`'s `steer`/`pause`/
    `resume`/`stop` methods do in production: translate the `RunControl`
    protocol's names onto the channel's writer-side methods. `RunApi`'s
    `session_provider` returns this so `_execute_command` can route through
    it exactly as it would a live session.
    """

    def __init__(self, channel: RunControlChannel) -> None:
        self._channel = channel

    def steer(self, text: str) -> None:
        self._channel.queue_steer(text)

    def pause(self) -> None:
        self._channel.request_pause()

    def resume(self) -> None:
        self._channel.resume()

    def stop(self) -> None:
        self._channel.request_stop()


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
    core_events: CoreEventJournal
    control: RunControlChannel

    def attach(
        self,
        log_dir: Path,
        *,
        project: Project | None = None,
        run_id: str | None = None,
    ) -> None:
        """Attach the integration and core event journal to durable state.

        Mirrors what `vibesys.run.integration.LocalRunIntegration.attach`
        does for its own `events` journal in production: this harness has no
        session, so `core_events` needs its own attach call to write
        ``core-events.jsonl`` under *log_dir*.
        """
        self.integration.attach(log_dir, project=project, run_id=run_id)
        self.core_events.attach(log_dir, run_id or log_dir.parent.name)

    def close(self) -> None:
        """Release subscriptions owned by the integration adapter."""
        self.integration.close()

    def publish_committed_view(
        self, view: RunView, changed_keys: tuple[str, ...] | None = None
    ) -> None:
        """Feed a projected view to the API as `RunSession.on_committed_view` would.

        Production wires this through `ServerRuntime.drive`
        (`session.on_committed_view(self.api._observe_committed_state)`); this
        harness has no session, so it calls the same method directly.
        """
        self.api._observe_committed_state(view, changed_keys)


def build_server_parts(
    log_dir: Path | None = None,
    *,
    project: Project | None = None,
    run_id: str | None = None,
    tui_defaults: Callable[[], InteractiveSetupDefaults] | None = None,
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
    core_events = CoreEventJournal()
    core_events.subscribe(integration.project_event)
    control = RunControlChannel(core_events)
    control_bridge = _ControlBridge(control)
    api = RunApi(
        condition,
        controller,
        executions,
        journal,
        chat,
        integration,
        session_provider=lambda: control_bridge,
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
        core_events=core_events,
        control=control,
    )
    if log_dir is not None:
        parts.attach(log_dir, project=project, run_id=run_id)
    return parts
