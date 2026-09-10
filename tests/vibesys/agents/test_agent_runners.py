"""Tests for application-level agent clients."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest


from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from vibesys.agent_runner import log_json_and_print, log_prompt_markdown_and_print
from vibesys.agents import build_agent_client
from vibesys.agents.callbacks import AgentLogger
from vibesys.agents.client import AgentClient
from vibesys.agents.deepagents_runner import DeepAgentsClient
from vibesys.agents.drivers.agentshim import AgentShimDriver
from vibesys.agents.progress import RoundProgress
from vibesys.agents.session_key import AgentSessionKey, SessionScope
from vibesys.config import Config
from vibesys.schemas import (
    IssueJudgeResponse,
    JudgeResponse,
    Verdict,
)
from vs_sandbox import ProjectPathPolicy


def _agent_config(**agent) -> Config:  # noqa: ANN003  # tracked: #288
    """Minimal valid Config carrying just an ``[agent]`` section for runner tests."""
    return Config.model_validate({"model": {"name": "m"}, "agent": agent})


def _judge_fallback() -> JudgeResponse:
    return JudgeResponse(
        analysis="fallback",
        feedback="fallback-feedback",
        verdict=Verdict.FAIL,
    )


def test_prompt_markdown_emitter_preserves_raw_log_and_truncates_stdout(capsys, headless_renderer):  # noqa: ANN001, ANN201  # tracked: #288
    headless_renderer.max_text_len = 20
    log = StringIO()
    prompt = "# Title\n\nUse **markdown** and `code`."

    log_prompt_markdown_and_print(prompt, log_file=log)

    stdout = capsys.readouterr().out
    assert "# Title" in stdout
    assert "... [17 more chars, see log for full text]" in stdout
    assert log.getvalue() == prompt + "\n"


def test_json_emitter_preserves_raw_log(capsys):  # noqa: ANN001, ANN201  # tracked: #288
    log = StringIO()
    raw_json = '{"analysis":"ok","items":[1,2]}'

    log_json_and_print(raw_json, log_file=log)

    stdout = capsys.readouterr().out
    assert raw_json in stdout
    assert log.getvalue() == raw_json + "\n"


class TestDeepAgentsClient:
    """Tests for :class:`DeepAgentsClient`."""

    def test_deepagents_runner_names_no_provider_conversation(self) -> None:
        runner = DeepAgentsClient(
            model="m",
            backends={"judge": MagicMock(name="judge-backend")},
            skills=[],
            model_name="m",
            run_log_file=None,
        )
        key = AgentSessionKey(SessionScope.CHAT, "thread-a")

        # A deepagents thread is a checkpointer entry, not a provider
        # conversation, and this client never runs ``AgentClient.__init__``, so
        # the inherited implementation would have no cache or store to read.
        assert runner.provider_session_id(key) is None
        assert runner.last_turn_provider_session_id(key) is None

    def test_deepagents_runner_invoke_returns_structured_response(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        pass_response = JudgeResponse(
            analysis="looks good",
            feedback="",
            verdict=Verdict.PASS,
        )
        with (
            patch("vibesys.agents.deepagents_runner.create_deep_agent") as mock_create,
            patch("vibesys.agents.deepagents_runner.run_typed_agent") as mock_run,
        ):
            mock_create.return_value = MagicMock(name="deep_agent")
            mock_run.return_value = pass_response

            runner = DeepAgentsClient(
                model="m",
                backends={
                    "implementer": MagicMock(name="impl-backend"),
                    "judge": MagicMock(name="judge-backend"),
                    "perf_eval": MagicMock(name="perf-backend"),
                },
                skills=[],
                model_name="m",
                run_log_file=None,
            )

            result = runner.invoke(
                kind="judge",
                workspace=tmp_path,
                system_prompt="sys",
                user_prompt="usr",
                response_cls=JudgeResponse,
                fallback_factory=_judge_fallback,
                round_label="judge #1",
                progress=RoundProgress(1, 5),
            )

        assert result is pass_response
        assert mock_run.call_count == 1
        _, kwargs = mock_run.call_args
        assert kwargs["response_cls"] is JudgeResponse
        assert kwargs["fallback_factory"] is _judge_fallback
        callbacks = kwargs["callbacks"]
        assert callbacks[0]._progress.label() == "Round 1/5"  # noqa: SLF001  # tracked: #288

    def test_deepagents_runner_picks_backend_by_kind(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        impl_backend = MagicMock(name="impl-backend")
        judge_backend = MagicMock(name="judge-backend")
        perf_backend = MagicMock(name="perf-backend")

        captured_backends: list = []

        def _capture(**kwargs):  # noqa: ANN003, ANN202  # tracked: #288
            captured_backends.append(kwargs["backend"])
            return MagicMock(name="deep_agent")

        with (
            patch(
                "vibesys.agents.deepagents_runner.create_deep_agent",
                side_effect=_capture,
            ),
            patch(
                "vibesys.agents.deepagents_runner.run_typed_agent",
                return_value=_judge_fallback(),
            ),
        ):
            runner = DeepAgentsClient(
                model="m",
                backends={
                    "implementer": impl_backend,
                    "judge": judge_backend,
                    "perf_eval": perf_backend,
                },
                skills=[],
                model_name="m",
                run_log_file=None,
            )

            for kind in ("implementer", "judge", "perf_eval"):
                runner.invoke(
                    kind=kind,
                    workspace=tmp_path,
                    system_prompt="sys",
                    user_prompt="usr",
                    response_cls=JudgeResponse,
                    fallback_factory=_judge_fallback,
                    round_label=f"{kind} #1",
                )

        assert captured_backends == [impl_backend, judge_backend, perf_backend]

    def test_deepagents_runner_returns_plain_text_without_response_format(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        with (
            patch("vibesys.agents.deepagents_runner.create_deep_agent") as mock_create,
            patch(
                "vibesys.agents.deepagents_runner.run_agent",
                return_value="Natural **Markdown** answer.",
            ) as mock_run,
        ):
            mock_create.return_value = MagicMock(name="chat-agent")
            runner = DeepAgentsClient(
                model="m",
                backends={"chat": MagicMock(name="chat-backend")},
                skills=[],
                model_name="m",
                run_log_file=None,
            )

            result = runner.invoke_text(
                kind="chat",
                workspace=tmp_path,
                system_prompt="investigate",
                user_prompt="what happened?",
                round_label="experiment chat",
            )

        assert result == "Natural **Markdown** answer."
        assert "response_format" not in mock_create.call_args.kwargs
        assert mock_run.call_args.args[1] == "what happened?"

    def test_deepagents_runner_sessions_are_explicit_and_role_scoped(self):  # noqa: ANN201  # tracked: #288
        runner = DeepAgentsClient(
            model="m",
            backends={},
            skills=[],
            model_name="m",
            run_log_file=None,
        )

        first = runner._session(  # noqa: SLF001  # tracked: #288
            kind="implementer",
            reuse_session=True,
            session_key=AgentSessionKey(SessionScope.HYPOTHESIS, "a"),
        )
        continued = runner._session(  # noqa: SLF001  # tracked: #288
            kind="implementer",
            reuse_session=True,
            session_key=AgentSessionKey(SessionScope.HYPOTHESIS, "a"),
        )
        other_role = runner._session(  # noqa: SLF001  # tracked: #288
            kind="judge",
            reuse_session=True,
            session_key=AgentSessionKey(SessionScope.HYPOTHESIS, "a"),
        )
        fresh = runner._session(  # noqa: SLF001  # tracked: #288
            kind="implementer",
            reuse_session=False,
            session_key=AgentSessionKey(SessionScope.HYPOTHESIS, "a"),
        )

        assert continued is first
        assert other_role is not first
        assert fresh is not first

    def test_deepagents_runner_reuses_graph_with_fresh_default_threads(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        """Repeated calls reuse construction but keep default conversations isolated."""
        pass_response = JudgeResponse(
            analysis="looks good",
            feedback="",
            verdict=Verdict.PASS,
        )
        with (
            patch("vibesys.agents.deepagents_runner.create_deep_agent") as mock_create,
            patch(
                "vibesys.agents.deepagents_runner.run_typed_agent",
                return_value=pass_response,
            ) as mock_run,
        ):
            mock_create.return_value = MagicMock(name="deep_agent")
            runner = DeepAgentsClient(
                model="m",
                backends={"judge": MagicMock(name="judge-backend")},
                skills=[],
                model_name="m",
                run_log_file=None,
            )

            for i in range(2):
                runner.invoke(
                    kind="judge",
                    workspace=tmp_path,
                    system_prompt="sys",
                    user_prompt=f"usr {i}",
                    response_cls=JudgeResponse,
                    fallback_factory=_judge_fallback,
                    round_label=f"judge #{i}",
                )

            assert mock_create.call_count == 1
            thread_ids = [call.kwargs["thread_id"] for call in mock_run.call_args_list]
            assert thread_ids[0] != thread_ids[1]

    def test_deepagents_runner_rebuilds_when_response_schema_changes(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        """A different response model must not reuse the old structured graph."""
        with (
            patch("vibesys.agents.deepagents_runner.create_deep_agent") as mock_create,
            patch(
                "vibesys.agents.deepagents_runner.run_typed_agent",
                return_value=JudgeResponse(analysis="ok", feedback="", verdict=Verdict.PASS),
            ),
        ):
            mock_create.return_value = MagicMock(name="deep_agent")
            runner = DeepAgentsClient(
                model="m",
                backends={"judge": MagicMock(name="judge-backend")},
                skills=[],
                model_name="m",
                run_log_file=None,
            )

            runner.invoke(
                kind="judge",
                workspace=tmp_path,
                system_prompt="sys",
                user_prompt="usr",
                response_cls=JudgeResponse,
                fallback_factory=_judge_fallback,
                round_label="judge #1",
            )
            runner.invoke(
                kind="judge",
                workspace=tmp_path,
                system_prompt="sys",
                user_prompt="usr",
                response_cls=IssueJudgeResponse,
                fallback_factory=lambda: IssueJudgeResponse(
                    issue_id=1,
                    analysis="fallback",
                    feedback="fallback",
                    verdict=Verdict.FAIL,
                ),
                round_label="judge #2",
            )

            assert mock_create.call_count == 2
            assert "response_format" in mock_create.call_args_list[0].kwargs
            assert "response_format" in mock_create.call_args_list[1].kwargs
            assert (
                mock_create.call_args_list[0].kwargs["response_format"].schema
                is not mock_create.call_args_list[1].kwargs["response_format"].schema
            )

    def test_deepagents_runner_rebuilds_when_tool_objects_change(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        """Same-named tools can carry different closure-bound behavior."""
        with (
            patch("vibesys.agents.deepagents_runner.create_deep_agent") as mock_create,
            patch(
                "vibesys.agents.deepagents_runner.run_typed_agent",
                return_value=JudgeResponse(analysis="ok", feedback="", verdict=Verdict.PASS),
            ),
        ):
            mock_create.return_value = MagicMock(name="deep_agent")
            runner = DeepAgentsClient(
                model="m",
                backends={"judge": MagicMock(name="judge-backend")},
                skills=[],
                model_name="m",
                run_log_file=None,
            )

            for tool in (MagicMock(name="same-tool"), MagicMock(name="same-tool")):
                runner.invoke(
                    kind="judge",
                    workspace=tmp_path,
                    system_prompt="sys",
                    user_prompt="usr",
                    response_cls=JudgeResponse,
                    fallback_factory=_judge_fallback,
                    round_label="judge",
                    tools=[tool],
                )

            assert mock_create.call_count == 2

    def test_deepagents_runner_typed_and_text_graphs_are_separate(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        with (
            patch("vibesys.agents.deepagents_runner.create_deep_agent") as mock_create,
            patch(
                "vibesys.agents.deepagents_runner.run_typed_agent",
                return_value=JudgeResponse(analysis="ok", feedback="", verdict=Verdict.PASS),
            ),
            patch(
                "vibesys.agents.deepagents_runner.run_agent",
                return_value="plain text",
            ),
        ):
            mock_create.return_value = MagicMock(name="deep_agent")
            runner = DeepAgentsClient(
                model="m",
                backends={"judge": MagicMock(name="judge-backend")},
                skills=[],
                model_name="m",
                run_log_file=None,
            )

            runner.invoke(
                kind="judge",
                workspace=tmp_path,
                system_prompt="sys",
                user_prompt="usr",
                response_cls=JudgeResponse,
                fallback_factory=_judge_fallback,
                round_label="judge",
            )
            runner.invoke_text(
                kind="judge",
                workspace=tmp_path,
                system_prompt="sys",
                user_prompt="usr",
                round_label="chat",
            )

            assert mock_create.call_count == 2
            assert "response_format" in mock_create.call_args_list[0].kwargs
            assert "response_format" not in mock_create.call_args_list[1].kwargs


class TestBuildAgentClient:
    """Tests for :func:`build_agent_client`."""

    def test_build_agent_client_default_is_cli(self):  # noqa: ANN201  # tracked: #288
        runner = build_agent_client(
            _agent_config(),
            agent_backend=None,
            cli_provider=None,
            backends={
                "implementer": MagicMock(),
                "judge": MagicMock(),
                "perf_eval": MagicMock(),
            },
            skills=[],
            skill_source_dirs=[],
            model="m",
            model_name="m",
            run_log_file=None,
            use_docker=False,
        )
        assert runner.backend_name == "cli"
        assert runner.provider == "codex"

    def test_build_agent_client_cli_provider_from_config(self):  # noqa: ANN201  # tracked: #288
        runner = build_agent_client(
            _agent_config(backend="cli", cli_provider="claude"),
            agent_backend=None,
            cli_provider=None,
            backends=None,
            skills=[],
            skill_source_dirs=[],
            model=None,
            model_name="m",
            run_log_file=None,
            use_docker=False,
        )
        assert runner.backend_name == "cli"
        assert runner.provider == "claude"

    def test_build_agent_client_cli_defaults_to_codex(self):  # noqa: ANN201  # tracked: #288
        """When backend=cli and no provider specified, defaults to codex."""
        runner = build_agent_client(
            _agent_config(backend="cli"),
            agent_backend=None,
            cli_provider=None,
            backends=None,
            skills=[],
            skill_source_dirs=[],
            model=None,
            model_name="m",
            run_log_file=None,
            use_docker=False,
        )
        assert runner.backend_name == "cli"
        assert runner.provider == "codex"

    def test_build_agent_client_cli_docker_returns_a_containerized_driver(self):  # noqa: ANN201  # tracked: #288
        """cli backend + docker returns an AgentClient over a containerized AgentShim driver."""
        from unittest.mock import MagicMock  # noqa: PLC0415  # tracked: #288

        mock_backends = {
            "implementer": MagicMock(),
            "judge": MagicMock(),
            "perf_eval": MagicMock(),
        }
        runner = build_agent_client(
            _agent_config(),
            agent_backend="cli",
            cli_provider="claude",
            backends=mock_backends,
            skills=[],
            skill_source_dirs=[],
            model=None,
            model_name="m",
            run_log_file=None,
            use_docker=True,
        )
        assert isinstance(runner, AgentClient)
        assert isinstance(runner._driver, AgentShimDriver)  # noqa: SLF001
        assert runner._driver._docker_sandboxes is mock_backends  # noqa: SLF001

    def test_build_agent_client_rejects_unsupported_docker_provider(self):  # noqa: ANN201  # tracked: #288
        with pytest.raises(SystemExit, match="not yet supported with --docker"):
            build_agent_client(
                _agent_config(),
                agent_backend="cli",
                cli_provider="nonexistent",
                backends={},
                skills=[],
                skill_source_dirs=[],
                model=None,
                model_name="m",
                run_log_file=None,
                use_docker=True,
            )

    def test_build_agent_client_rejects_unknown_backend(self):  # noqa: ANN201  # tracked: #288
        with pytest.raises(SystemExit, match="unknown agent backend"):
            build_agent_client(
                _agent_config(),
                agent_backend="bogus",
                cli_provider=None,
                backends=None,
                skills=[],
                skill_source_dirs=[],
                model=None,
                model_name="m",
                run_log_file=None,
                use_docker=False,
            )

    def test_required_project_enforcement_rejects_deepagents(self):  # noqa: ANN201  # tracked: #288
        with pytest.raises(SystemExit, match="requires the CLI agent backend"):
            build_agent_client(
                _agent_config(backend="deepagents"),
                agent_backend=None,
                cli_provider=None,
                backends={"implementer": MagicMock()},
                skills=[],
                skill_source_dirs=[],
                model="m",
                model_name="m",
                run_log_file=None,
                use_docker=False,
                require_host_sandbox=True,
            )

    def test_required_workspace_enforcement_permits_omnigent(self):  # noqa: ANN201
        config = Config.model_validate(
            {
                "model": {"name": "m"},
                "agent": {
                    "backend": "cli",
                    "cli_provider": "codex",
                    "driver": "omnigent",
                },
            }
        )

        runner = build_agent_client(
            config,
            agent_backend=None,
            cli_provider=None,
            backends=None,
            skills=[],
            skill_source_dirs=[],
            model=None,
            model_name="m",
            run_log_file=None,
            use_docker=False,
            require_host_sandbox=True,
        )

        assert isinstance(runner, AgentClient)
        assert type(runner._driver).__name__ == "OmnigentDriver"  # noqa: SLF001

    def test_required_project_enforcement_permits_stub(self):  # noqa: ANN201  # tracked: #288
        runner = build_agent_client(
            _agent_config(backend="stub"),
            agent_backend=None,
            cli_provider=None,
            backends=None,
            skills=[],
            skill_source_dirs=[],
            model=None,
            model_name="m",
            run_log_file=None,
            use_docker=False,
            require_host_sandbox=True,
        )

        assert runner.backend_name == "stub"

    def test_build_agent_client_forwards_project_policy_to_cli(self):  # noqa: ANN201  # tracked: #288
        policy = ProjectPathPolicy(
            read_only_paths=(".state",),
            hidden_paths=(".state/local",),
        )

        runner = build_agent_client(
            _agent_config(backend="cli", cli_provider="codex"),
            agent_backend=None,
            cli_provider=None,
            backends=None,
            skills=[],
            skill_source_dirs=[],
            model=None,
            model_name="m",
            run_log_file=None,
            use_docker=False,
            project_path_policy=policy,
            require_host_sandbox=True,
        )

        assert isinstance(runner, AgentClient)
        assert runner._policy.project_paths is policy  # noqa: SLF001
        assert runner._policy.require_enforcement is True  # noqa: SLF001

    # --- model resolution for the cli backend ---------------------------------
    #
    # Regression coverage for the config API where [model].name did not reach
    # the CLI tool. [model].name is the single source of truth: it must be the
    # model handed to the CLI tool, and the displayed model_name must equal it
    # so the run-log header can't report a model that isn't running.

    @staticmethod
    def _cli_client(config, *, model_name):  # noqa: ANN001, ANN205  # tracked: #288
        return build_agent_client(
            config,
            agent_backend=None,
            cli_provider=None,
            backends=None,
            skills=[],
            skill_source_dirs=[],
            model=None,
            model_name=model_name,
            run_log_file=None,
            use_docker=False,
        )

    @pytest.mark.parametrize("provider", ["claude", "gemini", "codex", "opencode"])
    def test_cli_backend_uses_model_name(self, provider):  # noqa: ANN001, ANN201  # tracked: #288
        runner = self._cli_client(
            _agent_config(backend="cli", cli_provider=provider),
            model_name="gpt-5.4",
        )
        assert runner._model_name == "gpt-5.4"  # noqa: SLF001  # tracked: #288

    def test_displayed_model_name_matches_model_passed(self):  # noqa: ANN201  # tracked: #288
        # The run-log header prints _model_name; it must equal the model
        # actually handed to the CLI tool so the log never reports a model
        # that isn't running.
        runner = self._cli_client(
            _agent_config(backend="cli", cli_provider="codex"),
            model_name="gpt-5.4",
        )
        assert runner._model_name == "gpt-5.4"  # noqa: SLF001  # tracked: #288

    def test_cli_backend_carries_outer_and_inner_role_configuration(self):  # noqa: ANN201  # tracked: #288
        config = _agent_config(
            backend="cli",
            cli_provider="codex",
            outer={"model": "gpt-5.6-sol", "reasoning_effort": "xhigh"},
            inner={"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"},
        )
        config.thinking.level = "high"
        runner = self._cli_client(config, model_name="gpt-5.6-sol")

        assert runner._default_reasoning_effort == "high"  # noqa: SLF001  # tracked: #288
        assert runner._role_models == {  # noqa: SLF001  # tracked: #288
            "orchestrator": "gpt-5.6-sol",
            "implementer": "gpt-5.6-luna",
        }
        assert runner._role_reasoning_efforts == {  # noqa: SLF001  # tracked: #288
            "orchestrator": "xhigh",
            "implementer": "xhigh",
        }


class TestAgentLoggerEventHandler:
    """Tests for :class:`AgentLogger` as a CLI event handler."""

    def test_agent_logger_event_handler_methods_drive_formatters(self):  # noqa: ANN201  # tracked: #288
        log_file = MagicMock()
        logger = AgentLogger(
            log_file=log_file,
            model_name="m",
            agent_label="Judge",
        )

        with (
            patch.object(logger, "log_tool_call") as mock_tool_call,
            patch.object(logger, "log_tool_result") as mock_tool_result,
        ):
            logger.on_thinking("hello")
            logger.on_tool_call("Bash", {"command": "ls"})
            logger.on_tool_result("Bash", stdout="output", exit_code=0)
            logger.on_tool_result("Bash", stderr="boom", exit_code=1)

        mock_tool_call.assert_called_once_with("Bash", {"command": "ls"})
        assert mock_tool_result.call_count == 2

        ok_call = mock_tool_result.call_args_list[0]
        assert ok_call.args[0] == "Bash"
        assert ok_call.args[1] == "output"
        assert ok_call.kwargs.get("is_error") is False

        err_call = mock_tool_result.call_args_list[1]
        assert err_call.args[0] == "Bash"
        assert err_call.args[1] == "boom"
        assert err_call.kwargs.get("is_error") is True

    def test_agent_logger_event_handler_forwards_usage(self):  # noqa: ANN201  # tracked: #288
        log_file = MagicMock()
        logger = AgentLogger(
            log_file=log_file,
            model_name="claude-sonnet-4-6",
            agent_label="Implementer",
        )

        usage = {
            "input_tokens": 12_345,
            "output_tokens": 67,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        logger.on_usage(usage)

        assert logger._input_tokens == 12_345  # noqa: SLF001  # tracked: #288
        assert logger._latest_usage == usage  # noqa: SLF001  # tracked: #288


class TestBuildAgentClientBackendSelection:
    """``build_agent_client`` backend resolution.

    The default agent backend is ``"cli"`` (provider ``"codex"``) when neither
    the ``--agent-backend`` flag nor an ``[agent].backend`` config key is set.
    Pinned here so the default cannot silently flip.
    """

    def _build(self, config, *, agent_backend=None, cli_provider=None):  # noqa: ANN001, ANN202  # tracked: #288
        return build_agent_client(
            config,
            agent_backend=agent_backend,
            cli_provider=cli_provider,
            backends=None,
            skills=[],
            skill_source_dirs=[],
            model=None,
            model_name="",
            run_log_file=None,
            use_docker=False,
        )

    def test_default_backend_is_cli_with_empty_config(self):  # noqa: ANN201  # tracked: #288
        runner = self._build(_agent_config())
        assert isinstance(runner, AgentClient)
        assert runner.provider == "codex"

    def test_empty_agent_section_defaults_to_cli(self):  # noqa: ANN201  # tracked: #288
        runner = self._build(_agent_config())
        assert isinstance(runner, AgentClient)
        assert runner.provider == "codex"

    def test_agent_backend_flag_overrides_config(self):  # noqa: ANN201  # tracked: #288
        # An explicit --agent-backend flag wins over [agent].backend.
        runner = self._build(_agent_config(backend="deepagents"), agent_backend="cli")
        assert isinstance(runner, AgentClient)

    def test_config_can_select_cli_provider(self):  # noqa: ANN201  # tracked: #288
        runner = self._build(_agent_config(backend="cli", cli_provider="claude"))
        assert isinstance(runner, AgentClient)
        assert runner.provider == "claude"
