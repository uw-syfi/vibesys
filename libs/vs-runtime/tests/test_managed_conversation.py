"""Public infrastructure contract for product-owned auxiliary conversations."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_agent.api import (
    NULL_AGENT_EVENT_SINK,
    NULL_SKILL_SELECTION,
    AgentBackend,
    AgentSessionKey,
    AgentSpec,
    SessionScope,
    SkillSelection,
)
from vs_agent.api.testing import FakeAgentClient
from vs_runtime.api.infrastructure import (
    ManagedConversationSpec,
    create_managed_conversation,
    open_managed_conversation,
)
from vs_sandbox.api import HostResource, ProjectPathPolicy

if TYPE_CHECKING:
    from vs_sandbox.api import Sandbox


@dataclass
class _CloseRecord:
    name: str
    order: list[str]

    def close(self) -> None:
        self.order.append(self.name)


class _ReplacingFakeAgentClient(FakeAgentClient):
    """Faithfully report that a continued turn ran in a replacement context."""

    def last_turn_provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        current = super().last_turn_provider_session_id(session_key)
        return f"replacement-for-{current}" if current is not None else None


class _ClientConstructionError(ValueError):
    """Scripted client factory failure."""

    def __init__(self) -> None:
        super().__init__("client construction failed")


@dataclass
class _Environment:
    """Deterministic agent environment transferred into a conversation."""

    close_error: BaseException | None = None
    close_count: int = 0
    skill_source_dirs: tuple[Path, ...] = ()
    skill_selection: SkillSelection = NULL_SKILL_SELECTION
    project_path_policy: ProjectPathPolicy = field(default_factory=ProjectPathPolicy)
    host_resources: tuple[HostResource, ...] = ()
    backends: dict[str, Sandbox] | None = None
    use_docker: bool = False

    def close(self) -> None:
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


def _spec() -> ManagedConversationSpec:
    return ManagedConversationSpec(
        role="chat",
        member_id="thread-1",
        workspace=Path("/workspace"),
        system_prompt="full rules",
        continuation_prompt="continue safely",
        environment=(("VIBESYS_STATE", "/agent/state"),),
    )


def test_same_object_continues_context_and_close_releases_resources_in_reverse() -> None:
    client = FakeAgentClient(session_reuse=True).enqueue_text("chat", "one", "two")
    close_order: list[str] = []
    conversation = create_managed_conversation(
        client,
        _spec(),
        resources=(
            _CloseRecord("environment", close_order),
            _CloseRecord("client", close_order),
        ),
    )

    assert conversation.turn("first", invocation_id="execution-1") == "one"
    assert conversation.turn("second") == "two"
    conversation.close()
    conversation.close()

    calls = client.calls_for("chat")
    assert [call.system_prompt for call in calls] == ["full rules", "continue safely"]
    assert [call.user_prompt for call in calls] == ["first", "second"]
    assert calls[0].invocation_id == "execution-1"
    assert calls[1].invocation_id is None
    assert all(call.env == {"VIBESYS_STATE": "/agent/state"} for call in calls)
    assert calls[0].session_key == calls[1].session_key
    assert calls[0].session_key is not None
    assert calls[0].session_key.scope is SessionScope.MEMBER
    assert calls[0].session_key.identifier.startswith("thread-1:")
    assert all(call.reuse_session is True for call in calls)
    assert close_order == ["client", "environment"]


def test_replaced_context_reasks_with_full_rules() -> None:
    client = _ReplacingFakeAgentClient(session_reuse=True)
    client.enqueue_text("chat", "established", "unsafe", "safe")
    conversation = create_managed_conversation(client, _spec(), resources=())

    assert conversation.turn("establish context") == "established"
    assert conversation.turn("inspect") == "safe"
    assert [call.system_prompt for call in client.calls_for("chat")] == [
        "full rules",
        "continue safely",
        "full rules",
    ]


def test_closed_conversation_rejects_new_turns() -> None:
    conversation = create_managed_conversation(FakeAgentClient(), _spec(), resources=())
    conversation.close()

    with pytest.raises(RuntimeError, match="closed"):
        conversation.turn("inspect")
    with pytest.raises(ValueError, match="must not be empty"):
        create_managed_conversation(FakeAgentClient(), _spec(), resources=()).turn("  ")


def test_new_conversation_with_same_member_starts_fresh() -> None:
    client = FakeAgentClient(session_reuse=True)
    first = create_managed_conversation(client, _spec(), resources=())
    second = create_managed_conversation(client, _spec(), resources=())

    first.turn("one")
    first.turn("two")
    second.turn("one")

    keys = [call.session_key for call in client.calls_for("chat")]
    assert keys[0] == keys[1]
    assert keys[2] != keys[0]
    assert [call.system_prompt for call in client.calls_for("chat")] == [
        "full rules",
        "continue safely",
        "full rules",
    ]


def test_construction_failure_leaves_resources_with_caller() -> None:
    close_order: list[str] = []
    resource = _CloseRecord("resource", close_order)
    invalid = ManagedConversationSpec(
        role="",
        member_id="thread-1",
        workspace=Path("/workspace"),
        system_prompt="rules",
    )

    with pytest.raises(ValueError, match="role"):
        create_managed_conversation(FakeAgentClient(), invalid, resources=(resource,))

    assert close_order == []
    resource.close()
    assert close_order == ["resource"]


def test_open_conversation_builds_client_and_owns_environment(tmp_path: Path) -> None:
    environment = _Environment(
        host_resources=(HostResource(tmp_path / "base"),),
    )
    additional = HostResource(tmp_path / "evidence")
    client = FakeAgentClient(session_reuse=True).enqueue_text("chat", "answer")
    observed: list[dict[str, object]] = []

    def build_client(**kwargs: object) -> FakeAgentClient:
        observed.append(kwargs)
        return client

    conversation = open_managed_conversation(
        _spec(),
        agent_spec=AgentSpec(backend=AgentBackend.STUB),
        environment=environment,
        log_directory=tmp_path,
        agent_events=NULL_AGENT_EVENT_SINK,
        additional_host_resources=(additional,),
        client_factory=build_client,
    )

    assert conversation.turn("inspect") == "answer"
    assert observed[0]["host_resources"] == (*environment.host_resources, additional)
    assert observed[0]["run_log_file"] is not None
    conversation.close()
    conversation.close()
    assert client.closed
    assert environment.close_count == 1


def test_open_conversation_preserves_construction_error_when_cleanup_fails(
    tmp_path: Path,
) -> None:
    environment = _Environment(close_error=RuntimeError("environment cleanup failed"))

    def fail_client(**kwargs: object) -> FakeAgentClient:
        del kwargs
        raise _ClientConstructionError

    with pytest.raises(ValueError, match="client construction failed") as failure:
        open_managed_conversation(
            _spec(),
            agent_spec=AgentSpec(backend=AgentBackend.STUB),
            environment=environment,
            log_directory=tmp_path,
            agent_events=NULL_AGENT_EVENT_SINK,
            client_factory=fail_client,
        )

    assert environment.close_count == 1
    assert any("environment cleanup failed" in note for note in failure.value.__notes__)
