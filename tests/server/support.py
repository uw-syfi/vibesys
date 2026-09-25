"""Test composition helpers for independently owned server components."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from server.api.service import RunApi
from server.chat.manager import ChatManager
from server.controller import RunController
from server.execution import AgentExecutionRequest, ExecutionHandle, ExecutionTracker
from server.integration import RunIntegrationAdapter
from server.journal import WireJournal
from server.read_model import RunInspector
from vibesys.agent_run.options import (
    AgentOrchestrationOptions,
    descriptor_from_options,
)
from vibesys.evaluators.metrics import MetricSpace
from vibesys.run.event_journal import EventJournal as CoreEventJournal
from vibesys.run.run_control import RunControlChannel

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from server.chat.factory import ChatAgentBuilder
    from server.settings import InteractiveSetupDefaults
    from vibesys.api import RunView
    from vs_agent.api import AgentSelection
    from vs_project.api import OrchestrationDescriptor, Project


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


def agent_descriptor(
    *,
    metric_space: MetricSpace | None = None,
) -> OrchestrationDescriptor:
    """Build the one active manifest descriptor for server agent fixtures."""
    options = AgentOrchestrationOptions(
        interface="inprocess",
        max_rounds=3,
        max_retries_per_round=1,
        judge_every=1,
        official_eval_every=1,
        memory_layout="files",
        metric_space=metric_space or MetricSpace(),
    )
    return descriptor_from_options(options, orchestration_id="single-agent")


@dataclass(frozen=True)
class ServerParts:
    """Explicitly composed server components used by focused tests."""

    condition: threading.Condition
    journal: WireJournal
    executions: ExecutionTracker
    controller: RunController
    chat: ChatManager
    integration: RunIntegrationAdapter
    api: RunApi
    core_events: CoreEventJournal
    control: RunControlChannel

    def start_execution(
        self,
        *args: str,
        participates_in_run_control: bool = True,
        emit_lifecycle: bool = True,
        agent_selection: AgentSelection | None = None,
    ) -> ExecutionHandle:
        """Build a lifecycle request for terse controller-focused tests."""
        if len(args) not in (3, 4):
            message = "start_execution expects kind, round, prompt, and optional system prompt"
            raise TypeError(message)
        kind, round_label, user_prompt = args[:3]
        system_prompt = args[3] if len(args) == 4 else ""
        return self.controller.start_agent_execution(
            AgentExecutionRequest(
                kind=kind,
                round_label=round_label,
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                participates_in_run_control=participates_in_run_control,
                emit_lifecycle=emit_lifecycle,
                driver=agent_selection.driver if agent_selection is not None else None,
                provider=agent_selection.provider if agent_selection is not None else None,
                model=agent_selection.model if agent_selection is not None else None,
            )
        )

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
        (`session.on_committed_view(self.api.observe_committed_state)`); this
        harness has no session, so it calls the same method directly.
        """
        self.api.observe_committed_state(view, changed_keys)


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
    journal = WireJournal(condition)
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
