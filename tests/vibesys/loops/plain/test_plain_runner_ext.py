"""Tests for PlainLoopAgentClient — the issue-loop's runner customization.

The wrapper sits in front of any AgentClient and injects issue-tracker
access for ``judge`` and ``perf_eval`` phases. Under the cli backend it
materializes an MCPServerSpec; under deepagents it materializes
LangChain ``@tool`` callables. Implementer phase passes through.
"""

from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from vibesys.agents.client import AgentClient
from vibesys.agents.contracts import AgentCapabilities, MCPServerSpec
from vibesys.agents.session_key import AgentSessionKey, SessionScope
from vibesys.loops.plain.runner_ext import PlainLoopAgentClient
from vs_issue_board import IssueBoard

_EXPECTED_TOOL_NAMES = {"list_issues", "get_issue", "search_issues", "create_issue"}


class _Resp(BaseModel):
    """Minimal response model for exercising the wrapper's typed invoke."""


def _mock_runner(backend_name: str) -> MagicMock:
    runner = MagicMock(spec=AgentClient)
    runner.backend_name = backend_name
    runner.capabilities = AgentCapabilities(
        mcp_servers=backend_name == "cli",
        in_process_tools=backend_name == "deepagents",
    )
    runner.invoke.return_value = "ok"
    return runner


def _make_store(tmp_path) -> IssueBoard:  # noqa: ANN001  # tracked: #288
    return IssueBoard(tmp_path / "issues.json")


def _tool_names(tools) -> set[str]:  # noqa: ANN001  # tracked: #288
    return {t.name for t in tools}


# ---------------------------------------------------------------------------
# deepagents path: wrapper builds in-process @tool callables
# ---------------------------------------------------------------------------


class TestDeepAgentsBackend:
    def test_judge_receives_in_process_tools(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        store = _make_store(tmp_path)
        inner = _mock_runner("deepagents")
        wrapper = PlainLoopAgentClient(inner, store=store, max_issues_per_perf_eval=3)

        wrapper.invoke(response_cls=_Resp, kind="judge", iteration=2, round_label="r")

        inner.invoke.assert_called_once()
        kwargs = inner.invoke.call_args.kwargs
        assert kwargs["kind"] == "judge"
        assert kwargs["mcp_servers"] is None
        assert _tool_names(kwargs["tools"]) == _EXPECTED_TOOL_NAMES

    def test_perf_eval_receives_in_process_tools(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        store = _make_store(tmp_path)
        inner = _mock_runner("deepagents")
        wrapper = PlainLoopAgentClient(inner, store=store, max_issues_per_perf_eval=4)

        wrapper.invoke(response_cls=_Resp, kind="perf_eval", iteration=5, round_label="r")

        kwargs = inner.invoke.call_args.kwargs
        assert kwargs["kind"] == "perf_eval"
        assert kwargs["mcp_servers"] is None
        assert _tool_names(kwargs["tools"]) == _EXPECTED_TOOL_NAMES

    def test_perf_eval_cap_comes_from_constructor(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        """The perf_eval cap reaches build_issue_tools verbatim — file 5
        issues and the 6th must be rejected with the cap-reached error."""
        store = _make_store(tmp_path)
        inner = _mock_runner("deepagents")
        wrapper = PlainLoopAgentClient(inner, store=store, max_issues_per_perf_eval=5)

        wrapper.invoke(response_cls=_Resp, kind="perf_eval", iteration=1, round_label="r")
        tools = inner.invoke.call_args.kwargs["tools"]
        create = next(t for t in tools if t.name == "create_issue")

        for i in range(5):
            out = create.invoke({"type": "perf", "title": f"t{i}", "description": "d"})
            assert "created" in out.lower() or "issue" in out.lower()

        sixth = create.invoke({"type": "perf", "title": "overflow", "description": "d"})
        assert "cap" in sixth.lower() or "limit" in sixth.lower()

    def test_judge_create_issue_rejects_non_bug_types(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        """Judge is bug-only — feature/perf must be rejected by policy."""
        store = _make_store(tmp_path)
        inner = _mock_runner("deepagents")
        wrapper = PlainLoopAgentClient(inner, store=store, max_issues_per_perf_eval=3)

        wrapper.invoke(response_cls=_Resp, kind="judge", iteration=1, round_label="r")
        tools = inner.invoke.call_args.kwargs["tools"]
        create = next(t for t in tools if t.name == "create_issue")

        for bad_type in ("feature", "perf"):
            out = create.invoke({"type": bad_type, "title": "x", "description": "d"})
            assert "type" in out.lower() or "allowed" in out.lower()


# ---------------------------------------------------------------------------
# cli path: wrapper builds an MCPServerSpec
# ---------------------------------------------------------------------------


class TestCliBackend:
    def test_judge_receives_mcp_server_spec(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        store = _make_store(tmp_path)
        inner = _mock_runner("cli")
        wrapper = PlainLoopAgentClient(inner, store=store, max_issues_per_perf_eval=3)

        wrapper.invoke(response_cls=_Resp, kind="judge", iteration=7, round_label="r")

        kwargs = inner.invoke.call_args.kwargs
        assert kwargs["tools"] is None
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

    def test_perf_eval_receives_mcp_server_spec_with_all_types(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        store = _make_store(tmp_path)
        inner = _mock_runner("cli")
        wrapper = PlainLoopAgentClient(inner, store=store, max_issues_per_perf_eval=4)

        wrapper.invoke(response_cls=_Resp, kind="perf_eval", iteration=2, round_label="r")

        kwargs = inner.invoke.call_args.kwargs
        assert kwargs["tools"] is None
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
        store = _make_store(tmp_path)
        inner = _mock_runner("cli")
        inner.invoke_text.return_value = "answer"
        wrapper = PlainLoopAgentClient(inner, store=store, max_issues_per_perf_eval=3)

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
            tools=None,
            reuse_session=None,
            session_key=None,
        )

    def test_implementer_passes_through_under_deepagents(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        store = _make_store(tmp_path)
        inner = _mock_runner("deepagents")
        wrapper = PlainLoopAgentClient(inner, store=store, max_issues_per_perf_eval=3)

        wrapper.invoke(response_cls=_Resp, kind="implementer", round_label="r")

        kwargs = inner.invoke.call_args.kwargs
        assert kwargs["kind"] == "implementer"
        assert kwargs["mcp_servers"] is None
        assert kwargs["tools"] is None

    def test_implementer_passes_through_under_cli(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        store = _make_store(tmp_path)
        inner = _mock_runner("cli")
        wrapper = PlainLoopAgentClient(inner, store=store, max_issues_per_perf_eval=3)

        wrapper.invoke(response_cls=_Resp, kind="implementer", round_label="r")

        kwargs = inner.invoke.call_args.kwargs
        assert kwargs["mcp_servers"] is None
        assert kwargs["tools"] is None

    def test_iteration_kwarg_is_consumed_not_forwarded(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        """The wrapper consumes ``iteration=`` and must not pass it to the
        inner client, whose public invoke API has no such kwarg."""
        store = _make_store(tmp_path)
        inner = _mock_runner("deepagents")
        wrapper = PlainLoopAgentClient(inner, store=store, max_issues_per_perf_eval=3)

        wrapper.invoke(response_cls=_Resp, kind="judge", iteration=1, round_label="r")

        assert "iteration" not in inner.invoke.call_args.kwargs

    def test_extra_kwargs_are_forwarded(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        """Caller-supplied kwargs (workspace, system_prompt, etc.) must
        reach the inner runner unchanged."""
        store = _make_store(tmp_path)
        inner = _mock_runner("deepagents")
        wrapper = PlainLoopAgentClient(inner, store=store, max_issues_per_perf_eval=3)

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
    def test_judge_without_iteration_raises(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        store = _make_store(tmp_path)
        wrapper = PlainLoopAgentClient(
            _mock_runner("deepagents"),
            store=store,
            max_issues_per_perf_eval=3,
        )
        with pytest.raises(ValueError, match="iteration"):
            wrapper.invoke(response_cls=_Resp, kind="judge", round_label="r")

    def test_perf_eval_without_iteration_raises(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        store = _make_store(tmp_path)
        wrapper = PlainLoopAgentClient(
            _mock_runner("cli"),
            store=store,
            max_issues_per_perf_eval=3,
        )
        with pytest.raises(ValueError, match="iteration"):
            wrapper.invoke(response_cls=_Resp, kind="perf_eval", round_label="r")


class TestBackendName:
    def test_backend_name_proxies_inner(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        store = _make_store(tmp_path)
        inner = _mock_runner("deepagents")
        wrapper = PlainLoopAgentClient(inner, store=store, max_issues_per_perf_eval=3)
        assert wrapper.backend_name == "deepagents"

        inner_cli = _mock_runner("cli")
        wrapper_cli = PlainLoopAgentClient(inner_cli, store=store, max_issues_per_perf_eval=3)
        assert wrapper_cli.backend_name == "cli"


class TestProviderConversation:
    def test_conversation_accessors_proxy_inner(self, tmp_path):  # noqa: ANN001, ANN201
        store = _make_store(tmp_path)
        inner = _mock_runner("cli")
        inner.provider_session_id.return_value = "session-1"
        inner.last_turn_provider_session_id.return_value = "session-2"
        wrapper = PlainLoopAgentClient(inner, store=store, max_issues_per_perf_eval=3)
        key = AgentSessionKey(SessionScope.CHAT, "thread-a")

        assert wrapper.provider_session_id(key) == "session-1"
        assert wrapper.last_turn_provider_session_id(key) == "session-2"
        inner.provider_session_id.assert_called_once_with(key)
        inner.last_turn_provider_session_id.assert_called_once_with(key)
