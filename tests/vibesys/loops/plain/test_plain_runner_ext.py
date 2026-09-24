"""Tests for PlainLoopAgentClient — the issue-loop's runner customization.

The wrapper sits in front of any AgentClient and injects issue-tracker
access for ``judge`` and ``perf_eval`` phases by materializing an
MCPServerSpec. Implementer phase passes through.
"""

from pathlib import Path

import pytest
from pydantic import BaseModel

from vibesys.loops.plain.runner_ext import PlainLoopAgentClient
from vs_agent.api import (
    AgentCapabilities,
    AgentSessionKey,
    MCPServerSpec,
    SessionScope,
)
from vs_agent.api.testing import FakeAgentClient


class _Resp(BaseModel):
    """Minimal response model for exercising the wrapper's typed invoke."""


# ---------------------------------------------------------------------------
# cli path: wrapper builds an MCPServerSpec
# ---------------------------------------------------------------------------


class TestCliBackend:
    def test_judge_receives_mcp_server_spec(self) -> None:
        inner = FakeAgentClient(
            backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True)
        )
        wrapper = PlainLoopAgentClient(inner, max_issues_per_perf_eval=3)

        wrapper.invoke(
            response_cls=_Resp,
            kind="judge",
            iteration=7,
            round_label="r",
            workspace=Path("/workspace"),
            system_prompt="sys",
            user_prompt="user",
            fallback_factory=_Resp,
        )

        judge_calls = inner.calls_for("judge")
        assert len(judge_calls) == 1
        specs = judge_calls[0].mcp_servers
        assert specs is not None
        assert len(specs) == 1
        spec = specs[0]
        assert isinstance(spec, MCPServerSpec)
        assert "--creator" in spec.args
        assert "judge" in spec.args
        assert "--cap" in spec.args
        assert "1" in spec.args
        assert "--iteration" in spec.args
        assert "7" in spec.args
        # judge is bug-only
        i_at = spec.args.index("--allowed-types")
        assert spec.args[i_at + 1] == "bug"

    def test_perf_eval_receives_mcp_server_spec_with_all_types(self) -> None:
        inner = FakeAgentClient(
            backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True)
        )
        wrapper = PlainLoopAgentClient(inner, max_issues_per_perf_eval=4)

        wrapper.invoke(
            response_cls=_Resp,
            kind="perf_eval",
            iteration=2,
            round_label="r",
            workspace=Path("/workspace"),
            system_prompt="sys",
            user_prompt="user",
            fallback_factory=_Resp,
        )

        perf_calls = inner.calls_for("perf_eval")
        assert len(perf_calls) == 1
        specs = perf_calls[0].mcp_servers
        assert specs is not None
        spec = specs[0]
        assert "perf_eval" in spec.args
        i_cap = spec.args.index("--cap")
        assert spec.args[i_cap + 1] == "4"
        i_at = spec.args.index("--allowed-types")
        # bug,feature,perf in alphabetical order (sorted by build_issue_mcp_spec)
        assert spec.args[i_at + 1] == "bug,feature,perf"


# ---------------------------------------------------------------------------
# implementer + miscellaneous behavior
# ---------------------------------------------------------------------------


class TestPassThrough:
    def test_text_turn_delegates_to_inner_client(self, tmp_path: Path) -> None:
        inner = FakeAgentClient(
            backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True)
        )
        inner.set_text("chat", "answer")
        wrapper = PlainLoopAgentClient(inner, max_issues_per_perf_eval=3)

        result = wrapper.invoke_text(
            kind="chat",
            workspace=tmp_path,
            system_prompt="system",
            user_prompt="question",
            round_label="chat",
        )

        assert result == "answer"
        calls = inner.calls_for("chat")
        assert len(calls) == 1
        call = calls[0]
        assert call.method == "invoke_text"
        assert call.workspace == tmp_path
        assert call.system_prompt == "system"
        assert call.user_prompt == "question"
        assert call.round_label == "chat"
        assert call.env is None
        assert call.invocation_id is None
        assert call.progress is None
        assert call.mcp_servers is None
        assert call.reuse_session is None
        assert call.session_key is None

    def test_implementer_passes_through(self) -> None:
        inner = FakeAgentClient(
            backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True)
        )
        wrapper = PlainLoopAgentClient(inner, max_issues_per_perf_eval=3)

        wrapper.invoke(
            response_cls=_Resp,
            kind="implementer",
            round_label="r",
            workspace=Path("/workspace"),
            system_prompt="sys",
            user_prompt="user",
            fallback_factory=_Resp,
        )

        impl_calls = inner.calls_for("implementer")
        assert len(impl_calls) == 1
        assert impl_calls[0].mcp_servers is None

    def test_iteration_kwarg_is_consumed_not_forwarded(self) -> None:
        """The wrapper consumes ``iteration=`` and must not pass it to the
        inner client, whose public invoke API has no such kwarg.

        ``FakeAgentClient.invoke`` has no ``iteration`` parameter, so a leak
        would already raise ``TypeError`` from the call below.
        """
        inner = FakeAgentClient(
            backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True)
        )
        wrapper = PlainLoopAgentClient(inner, max_issues_per_perf_eval=3)

        wrapper.invoke(
            response_cls=_Resp,
            kind="judge",
            iteration=1,
            round_label="r",
            workspace=Path("/workspace"),
            system_prompt="sys",
            user_prompt="user",
            fallback_factory=_Resp,
        )

        assert inner.calls_for("judge")

    def test_extra_kwargs_are_forwarded(self) -> None:
        """Caller-supplied kwargs (workspace, system_prompt, etc.) must
        reach the inner runner unchanged."""
        inner = FakeAgentClient(
            backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True)
        )
        wrapper = PlainLoopAgentClient(inner, max_issues_per_perf_eval=3)

        wrapper.invoke(
            response_cls=_Resp,
            kind="judge",
            iteration=1,
            workspace="/workspace",
            system_prompt="sys",
            user_prompt="user",
            round_label="r",
            fallback_factory=_Resp,
        )

        call = inner.calls_for("judge")[0]
        assert call.workspace == "/workspace"
        assert call.system_prompt == "sys"
        assert call.user_prompt == "user"
        assert call.round_label == "r"


class TestValidation:
    def test_judge_without_iteration_raises(self) -> None:
        wrapper = PlainLoopAgentClient(
            FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True)),
            max_issues_per_perf_eval=3,
        )
        with pytest.raises(ValueError, match="iteration"):
            wrapper.invoke(response_cls=_Resp, kind="judge", round_label="r")

    def test_perf_eval_without_iteration_raises(self) -> None:
        wrapper = PlainLoopAgentClient(
            FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True)),
            max_issues_per_perf_eval=3,
        )
        with pytest.raises(ValueError, match="iteration"):
            wrapper.invoke(response_cls=_Resp, kind="perf_eval", round_label="r")


class TestBackendName:
    def test_backend_name_proxies_inner(self) -> None:
        wrapper = PlainLoopAgentClient(
            FakeAgentClient(backend_name="cli"), max_issues_per_perf_eval=3
        )
        assert wrapper.backend_name == "cli"


class TestCapabilities:
    def test_client_without_mcp_cannot_host_tracker_tools(self) -> None:
        wrapper = PlainLoopAgentClient(
            FakeAgentClient(backend_name="stub"), max_issues_per_perf_eval=3
        )
        with pytest.raises(RuntimeError, match="cannot expose issue-board tools"):
            wrapper.invoke(response_cls=_Resp, kind="judge", iteration=1, round_label="r")


class TestProviderConversation:
    def test_conversation_accessors_proxy_inner(self) -> None:
        inner = FakeAgentClient(
            backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True)
        )
        key = AgentSessionKey(SessionScope.CHAT, "thread-a")
        inner.set_session(key, provider_session_id="session-1", last_turn="session-2")
        wrapper = PlainLoopAgentClient(inner, max_issues_per_perf_eval=3)

        assert wrapper.provider_session_id(key) == "session-1"
        assert wrapper.last_turn_provider_session_id(key) == "session-2"
