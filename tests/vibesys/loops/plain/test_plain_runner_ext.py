"""Tests for PlainLoopAgentClient — the issue-loop's runner customization.

The wrapper sits in front of any AgentClient and injects issue-tracker
access for ``judge`` and ``perf_eval`` phases by materializing an
MCPServerSpec. Implementer phase passes through.
"""

from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from vibesys.loops.plain.runner_ext import PlainLoopAgentClient
from vs_agent.client import AgentClient
from vs_agent.contracts import AgentCapabilities, MCPServerSpec
from vs_agent.session_key import AgentSessionKey, SessionScope


class _Resp(BaseModel):
    """Minimal response model for exercising the wrapper's typed invoke."""


def _mock_runner(backend_name: str) -> MagicMock:
    runner = MagicMock(spec=AgentClient)
    runner.backend_name = backend_name
    runner.capabilities = AgentCapabilities(
        mcp_servers=backend_name == "cli",
    )
    runner.invoke.return_value = "ok"
    return runner


# ---------------------------------------------------------------------------
# cli path: wrapper builds an MCPServerSpec
# ---------------------------------------------------------------------------


class TestCliBackend:
    def test_judge_receives_mcp_server_spec(self):  # noqa: ANN201
        inner = _mock_runner("cli")
        wrapper = PlainLoopAgentClient(inner, max_issues_per_perf_eval=3)

        wrapper.invoke(response_cls=_Resp, kind="judge", iteration=7, round_label="r")

        kwargs = inner.invoke.call_args.kwargs
        specs = kwargs["mcp_servers"]
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

    def test_perf_eval_receives_mcp_server_spec_with_all_types(self):  # noqa: ANN201
        inner = _mock_runner("cli")
        wrapper = PlainLoopAgentClient(inner, max_issues_per_perf_eval=4)

        wrapper.invoke(response_cls=_Resp, kind="perf_eval", iteration=2, round_label="r")

        kwargs = inner.invoke.call_args.kwargs
        spec = kwargs["mcp_servers"][0]
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
    def test_text_turn_delegates_to_inner_client(self, tmp_path):  # noqa: ANN001, ANN201
        inner = _mock_runner("cli")
        inner.invoke_text.return_value = "answer"
        wrapper = PlainLoopAgentClient(inner, max_issues_per_perf_eval=3)

        result = wrapper.invoke_text(
            kind="chat",
            workspace=tmp_path,
            system_prompt="system",
            user_prompt="question",
            round_label="chat",
        )

        assert result == "answer"
        inner.invoke_text.assert_called_once_with(
            kind="chat",
            workspace=tmp_path,
            system_prompt="system",
            user_prompt="question",
            round_label="chat",
            env=None,
            invocation_id=None,
            progress=None,
            mcp_servers=None,
            reuse_session=None,
            session_key=None,
        )

    def test_implementer_passes_through(self):  # noqa: ANN201
        inner = _mock_runner("cli")
        wrapper = PlainLoopAgentClient(inner, max_issues_per_perf_eval=3)

        wrapper.invoke(response_cls=_Resp, kind="implementer", round_label="r")

        kwargs = inner.invoke.call_args.kwargs
        assert kwargs["kind"] == "implementer"
        assert kwargs["mcp_servers"] is None

    def test_iteration_kwarg_is_consumed_not_forwarded(self):  # noqa: ANN201
        """The wrapper consumes ``iteration=`` and must not pass it to the
        inner client, whose public invoke API has no such kwarg."""
        inner = _mock_runner("cli")
        wrapper = PlainLoopAgentClient(inner, max_issues_per_perf_eval=3)

        wrapper.invoke(response_cls=_Resp, kind="judge", iteration=1, round_label="r")

        assert "iteration" not in inner.invoke.call_args.kwargs

    def test_extra_kwargs_are_forwarded(self):  # noqa: ANN201
        """Caller-supplied kwargs (workspace, system_prompt, etc.) must
        reach the inner runner unchanged."""
        inner = _mock_runner("cli")
        wrapper = PlainLoopAgentClient(inner, max_issues_per_perf_eval=3)

        wrapper.invoke(
            response_cls=_Resp,
            kind="judge",
            iteration=1,
            workspace="/tmp/ws",  # noqa: S108  # tracked: #288
            system_prompt="sys",
            user_prompt="user",
            round_label="r",
        )

        kwargs = inner.invoke.call_args.kwargs
        assert kwargs["workspace"] == "/tmp/ws"  # noqa: S108  # tracked: #288
        assert kwargs["system_prompt"] == "sys"
        assert kwargs["user_prompt"] == "user"
        assert kwargs["round_label"] == "r"


class TestValidation:
    def test_judge_without_iteration_raises(self):  # noqa: ANN201
        wrapper = PlainLoopAgentClient(
            _mock_runner("cli"),
            max_issues_per_perf_eval=3,
        )
        with pytest.raises(ValueError, match="iteration"):
            wrapper.invoke(response_cls=_Resp, kind="judge", round_label="r")

    def test_perf_eval_without_iteration_raises(self):  # noqa: ANN201
        wrapper = PlainLoopAgentClient(
            _mock_runner("cli"),
            max_issues_per_perf_eval=3,
        )
        with pytest.raises(ValueError, match="iteration"):
            wrapper.invoke(response_cls=_Resp, kind="perf_eval", round_label="r")


class TestBackendName:
    def test_backend_name_proxies_inner(self):  # noqa: ANN201
        wrapper = PlainLoopAgentClient(_mock_runner("cli"), max_issues_per_perf_eval=3)
        assert wrapper.backend_name == "cli"


class TestCapabilities:
    def test_client_without_mcp_cannot_host_tracker_tools(self):  # noqa: ANN201
        wrapper = PlainLoopAgentClient(_mock_runner("stub"), max_issues_per_perf_eval=3)
        with pytest.raises(RuntimeError, match="cannot expose issue-board tools"):
            wrapper.invoke(response_cls=_Resp, kind="judge", iteration=1, round_label="r")


class TestProviderConversation:
    def test_conversation_accessors_proxy_inner(self):  # noqa: ANN201
        inner = _mock_runner("cli")
        inner.provider_session_id.return_value = "session-1"
        inner.last_turn_provider_session_id.return_value = "session-2"
        wrapper = PlainLoopAgentClient(inner, max_issues_per_perf_eval=3)
        key = AgentSessionKey(SessionScope.CHAT, "thread-a")

        assert wrapper.provider_session_id(key) == "session-1"
        assert wrapper.last_turn_provider_session_id(key) == "session-2"
        inner.provider_session_id.assert_called_once_with(key)
        inner.last_turn_provider_session_id.assert_called_once_with(key)
