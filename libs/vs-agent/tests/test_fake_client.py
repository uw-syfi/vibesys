"""Unit tests for :class:`~vs_agent.fake_client.FakeAgentClient`.

Each test exercises one capability described in the fake's docstring/spec:
zero-config scripted returns, the enqueue/constant/fallback resolution order,
callable and dict responses, failures, attribution and model overrides, call
recording, streamed output, ``on_invoke`` side effects, session reuse, and
``invoke_text``'s parallel behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, Unpack, override

import pytest
from pydantic import BaseModel

from vs_agent.contracts import AgentCapabilities
from vs_agent.fake_client import FakeAgentClient, FakeInvocation
from vs_agent.session_key import AgentSessionKey, SessionScope
from vs_agent.sink import AgentEventSink

if TYPE_CHECKING:
    from vs_agent.api import AgentProgress, MCPServerSpec
    from vs_agent.events import AgentOutputChannel, AgentStatusData, TodoItemData, ToolResultPayload


class _Response(BaseModel):
    """A tiny local structured-response model, standing in for a real schema."""

    verdict: str
    detail: str = ""


class _CapturingSink(AgentEventSink):
    """Minimal :class:`AgentEventSink` that records every ``agent_output`` call."""

    def __init__(self) -> None:
        self.outputs: list[tuple[str, AgentOutputChannel, str | None, str | None]] = []

    @override
    def agent_output(
        self,
        content: str,
        *,
        channel: AgentOutputChannel = "assistant",
        status: AgentStatusData | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        del status, invocation_id
        self.outputs.append((content, channel, agent_kind, round_label))

    @override
    def tool_call(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        call_id: str | None = None,
        status: AgentStatusData | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        del tool, args, call_id, status, agent_kind, round_label, invocation_id

    @override
    def tool_result(
        self,
        tool: str,
        content: str,
        *,
        call_id: str | None = None,
        is_error: bool = False,
        payload: ToolResultPayload | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        del tool, content, call_id, is_error, payload, agent_kind, round_label, invocation_id

    def todo_update(
        self,
        todos: list[TodoItemData],
        *,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        del todos, agent_kind, round_label, invocation_id

    @override
    def usage_update(
        self,
        input_tokens: int,
        *,
        context_window: int | None = None,
        model: str | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        del input_tokens, context_window, model, agent_kind, round_label, invocation_id


class _InvokeOptions(TypedDict, total=False):
    """Optional invocation inputs shared by structured and text turns."""

    env: dict[str, str] | None
    invocation_id: str | None
    progress: AgentProgress | None
    mcp_servers: list[MCPServerSpec] | None
    reuse_session: bool | None
    session_key: AgentSessionKey | None


def _fallback() -> _Response:
    return _Response(verdict="fallback")


def _invoke(
    client: FakeAgentClient,
    *,
    kind: str = "judge",
    round_label: str = "round 4",
    **kwargs: Unpack[_InvokeOptions],
) -> _Response:
    return client.invoke(
        kind=kind,
        workspace=Path("workspace"),
        system_prompt="system",
        user_prompt="user",
        response_cls=_Response,
        fallback_factory=_fallback,
        round_label=round_label,
        **kwargs,
    )


def _invoke_text(
    client: FakeAgentClient,
    *,
    kind: str = "judge",
    round_label: str = "round 4",
    **kwargs: Unpack[_InvokeOptions],
) -> str:
    return client.invoke_text(
        kind=kind,
        workspace=Path("workspace"),
        system_prompt="system",
        user_prompt="user",
        round_label=round_label,
        **kwargs,
    )


def test_zero_config_invoke_falls_back_to_fallback_factory_for_unscripted_model() -> None:
    client = FakeAgentClient()

    response = _invoke(client)

    assert response == _Response(verdict="fallback")


def test_zero_config_invoke_returns_scripted_payload_for_known_response_model() -> None:
    client = FakeAgentClient()

    class JudgeResponse(BaseModel):
        analysis: str
        feedback: str
        verdict: str

    response = client.invoke(
        kind="judge",
        workspace=Path("workspace"),
        system_prompt="s",
        user_prompt="u",
        response_cls=JudgeResponse,
        fallback_factory=lambda: JudgeResponse(analysis="", feedback="", verdict="fail"),
        round_label="round 2",
    )

    assert response.verdict == "pass"


def test_enqueue_pops_responses_in_order_then_falls_back_to_constant() -> None:
    client = FakeAgentClient()
    client.enqueue("judge", _Response(verdict="first"), _Response(verdict="second"))
    client.set_response("judge", _Response(verdict="constant"))

    assert _invoke(client).verdict == "first"
    assert _invoke(client).verdict == "second"
    assert _invoke(client).verdict == "constant"
    assert _invoke(client).verdict == "constant"


def test_enqueue_accepts_a_callable_computed_from_the_invocation() -> None:
    client = FakeAgentClient()
    client.enqueue("judge", lambda invocation: _Response(verdict=invocation.kind))

    assert _invoke(client, kind="judge").verdict == "judge"


def test_enqueue_accepts_a_dict_validated_against_response_cls() -> None:
    client = FakeAgentClient()
    client.enqueue("judge", {"verdict": "from-dict", "detail": "validated"})

    response = _invoke(client)

    assert response == _Response(verdict="from-dict", detail="validated")


def test_fail_raises_forever_when_times_is_none() -> None:
    client = FakeAgentClient()
    client.fail("judge", RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        _invoke(client)
    with pytest.raises(RuntimeError, match="boom"):
        _invoke(client)


def test_fail_raises_exactly_times_then_resumes() -> None:
    client = FakeAgentClient()
    client.fail("judge", RuntimeError("boom"), times=2)
    client.set_response("judge", _Response(verdict="resumed"))

    with pytest.raises(RuntimeError, match="boom"):
        _invoke(client)
    with pytest.raises(RuntimeError, match="boom"):
        _invoke(client)
    assert _invoke(client).verdict == "resumed"


def test_set_attribution_overrides_only_given_fields() -> None:
    client = FakeAgentClient(driver_name="fake", provider="fake", model="fake-model")

    client.set_attribution(provider="anthropic")

    assert client.driver_name == "fake"
    assert client.provider == "anthropic"
    assert client.model_for_kind("judge") == "fake-model"


def test_set_model_for_kind_overrides_per_kind_falling_back_to_ctor_model() -> None:
    client = FakeAgentClient(model="default-model")
    client.set_model_for_kind({"judge": "judge-model"})

    assert client.model_for_kind("judge") == "judge-model"
    assert client.model_for_kind("implementer") == "default-model"


def test_calls_records_every_kwarg_and_calls_for_filters_by_kind() -> None:
    client = FakeAgentClient()
    key = AgentSessionKey(SessionScope.HYPOTHESIS, "H-01")

    _invoke(
        client,
        kind="judge",
        round_label="round 3",
        env={"A": "1"},
        invocation_id="inv-1",
        mcp_servers=None,
        reuse_session=True,
        session_key=key,
    )
    _invoke_text(client, kind="implementer", round_label="round 3", invocation_id="inv-2")

    assert len(client.calls) == 2
    judge_call = client.calls_for("judge")
    assert len(judge_call) == 1
    record = judge_call[0]
    assert isinstance(record, FakeInvocation)
    assert record.method == "invoke"
    assert record.kind == "judge"
    assert record.workspace == Path("workspace")
    assert record.system_prompt == "system"
    assert record.user_prompt == "user"
    assert record.round_label == "round 3"
    assert record.response_cls is _Response
    assert record.env == {"A": "1"}
    assert record.invocation_id == "inv-1"
    assert record.mcp_servers is None
    assert record.reuse_session is True
    assert record.session_key == key

    text_call = client.calls_for("implementer")[0]
    assert text_call.method == "invoke_text"
    assert text_call.response_cls is None


def test_stream_output_emits_through_the_event_sink() -> None:
    sink = _CapturingSink()
    client = FakeAgentClient(event_sink=sink)
    client.stream_output("judge", ["chunk-one", "chunk-two"])

    _invoke(client, kind="judge", round_label="round 5")

    assert sink.outputs == [
        ("chunk-one", "assistant", "judge", "round 5"),
        ("chunk-two", "assistant", "judge", "round 5"),
    ]


def test_on_invoke_callback_can_write_into_the_invocation_workspace(tmp_path: Path) -> None:
    client = FakeAgentClient()
    written: list[Path] = []

    def write_marker(invocation: FakeInvocation) -> None:
        marker = invocation.workspace / "marker.txt"
        marker.write_text(invocation.kind)
        written.append(marker)

    client.on_invoke(write_marker)

    client.invoke(
        kind="judge",
        workspace=tmp_path,
        system_prompt="s",
        user_prompt="u",
        response_cls=_Response,
        fallback_factory=_fallback,
        round_label="round 1",
    )

    assert written == [tmp_path / "marker.txt"]
    assert (tmp_path / "marker.txt").read_text() == "judge"


def test_session_mints_once_and_reuses_on_matching_key_when_enabled() -> None:
    client = FakeAgentClient(session_reuse=True)
    key = AgentSessionKey(SessionScope.HYPOTHESIS, "H-01")

    assert client.provider_session_id(key) is None

    _invoke(client, round_label="round 1", reuse_session=True, session_key=key)
    first_id = client.provider_session_id(key)
    assert first_id is not None
    assert client.last_turn_provider_session_id(key) == first_id

    _invoke(client, round_label="round 2", reuse_session=True, session_key=key)
    assert client.provider_session_id(key) == first_id
    assert client.last_turn_provider_session_id(key) == first_id


def test_session_stays_fresh_without_reuse_session_or_when_capability_disabled() -> None:
    client = FakeAgentClient(session_reuse=False)
    key = AgentSessionKey(SessionScope.HYPOTHESIS, "H-01")

    _invoke(client, reuse_session=True, session_key=key)
    assert client.provider_session_id(key) is None

    reusable_client = FakeAgentClient(session_reuse=True)
    _invoke(reusable_client, reuse_session=False, session_key=key)
    assert reusable_client.provider_session_id(key) is None

    _invoke(reusable_client, reuse_session=True, session_key=None)
    assert reusable_client.provider_session_id(key) is None


def test_set_session_seeds_ids_without_an_invoke() -> None:
    client = FakeAgentClient(session_reuse=True)
    key = AgentSessionKey(SessionScope.CHAT, "thread-a")

    client.set_session(key, provider_session_id="seeded-id", last_turn="seeded-last")

    assert client.provider_session_id(key) == "seeded-id"
    assert client.last_turn_provider_session_id(key) == "seeded-last"


def test_close_is_idempotent() -> None:
    client = FakeAgentClient()

    client.close()
    client.close()


def test_invoke_text_default_and_enqueue() -> None:
    client = FakeAgentClient()

    assert _invoke_text(client) == "Fake agent inspected the trajectory."

    client.enqueue_text("chat", "first reply", "second reply")
    assert _invoke_text(client, kind="chat") == "first reply"
    assert _invoke_text(client, kind="chat") == "second reply"

    client.set_text("chat", "constant reply")
    assert _invoke_text(client, kind="chat") == "constant reply"

    client.set_text(None, "new module-wide default")
    assert _invoke_text(client, kind="unconfigured") == "new module-wide default"


def test_invoke_text_records_calls_and_can_fail() -> None:
    client = FakeAgentClient()
    client.fail("chat", RuntimeError("text boom"), times=1)

    with pytest.raises(RuntimeError, match="text boom"):
        _invoke_text(client, kind="chat")

    assert _invoke_text(client, kind="chat") == "Fake agent inspected the trajectory."


def test_close_is_observable_and_idempotent() -> None:
    client = FakeAgentClient()
    assert client.closed is False

    client.close()
    client.close()

    assert client.closed is True


def test_set_log_file_records_the_wired_streams() -> None:
    client = FakeAgentClient()
    stream = object()

    client.set_log_file(stream)
    client.set_log_file(None)

    assert client.log_files == [stream, None]


def test_capabilities_and_backend_name_are_configurable() -> None:
    default = FakeAgentClient()
    assert default.backend_name == "fake"
    assert default.capabilities.mcp_servers is False

    client = FakeAgentClient(
        backend_name="cli",
        capabilities=AgentCapabilities(mcp_servers=True, session_reuse=True),
    )
    assert client.backend_name == "cli"
    assert client.capabilities.mcp_servers is True
    assert client.capabilities.session_reuse is True

    client.set_capabilities(AgentCapabilities(mcp_servers=False))
    assert client.capabilities.mcp_servers is False


def test_fail_can_raise_a_base_exception() -> None:
    client = FakeAgentClient()
    client.fail("chat", KeyboardInterrupt(), times=1)

    with pytest.raises(KeyboardInterrupt):
        _invoke_text(client, kind="chat")

    assert _invoke_text(client, kind="chat") == "Fake agent inspected the trajectory."


def test_enqueue_parse_failure_returns_the_fallback_positionally() -> None:
    client = FakeAgentClient()
    # First judge turn succeeds; the second returns unparseable output so the
    # loop falls back. Both turns are still recorded.
    client.enqueue("judge", _Response(verdict="pass"))
    client.enqueue_parse_failure("judge")

    assert _invoke(client) == _Response(verdict="pass")
    assert _invoke(client) == _Response(verdict="fallback")
    assert len(client.calls_for("judge")) == 2


def test_evict_session_clears_a_seeded_conversation() -> None:
    client = FakeAgentClient(session_reuse=True)
    key = AgentSessionKey(SessionScope.CHAT, "thread-a")
    client.set_session(key, provider_session_id="session-1", last_turn="session-1")
    assert client.provider_session_id(key) == "session-1"

    client.evict_session(key)

    assert client.provider_session_id(key) is None
    assert client.last_turn_provider_session_id(key) is None
