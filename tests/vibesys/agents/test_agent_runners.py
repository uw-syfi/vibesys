"""Tests for application-level agent clients."""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from vibesys.agent_spec_config import agent_spec_from_config
from vibesys.config import Config
from vibesys.render.log import log_json_and_print, log_prompt_markdown_and_print
from vibesys.schemas import (
    JudgeResponse,
    Verdict,
)
from vs_agent.api import AgentClient, build_agent_client
from vs_agent.api.testing import AgentLogger
from vs_agent.drivers.agentshim import AgentShimDriver
from vs_sandbox import ProjectPathPolicy


def _agent_config(**agent) -> Config:  # noqa: ANN003  # tracked: #288
    """Minimal valid Config carrying just an ``[agent]`` section for runner tests."""
    return Config.model_validate({"model": {"name": "m"}, "agent": agent})


def _build_client(  # noqa: ANN202, PLR0913
    config,  # noqa: ANN001  # tracked: #288
    *,
    agent_backend=None,  # noqa: ANN001  # tracked: #288
    cli_provider=None,  # noqa: ANN001  # tracked: #288
    model_name="m",  # noqa: ANN001  # tracked: #288
    backends=None,  # noqa: ANN001  # tracked: #288
    skill_source_dirs=None,  # noqa: ANN001  # tracked: #288
    use_docker=False,  # noqa: ANN001  # tracked: #288
    **kwargs,  # noqa: ANN003  # tracked: #288
):
    """Resolve an :class:`AgentSpec` the way application config does, then build."""
    spec = agent_spec_from_config(
        config,
        backend=agent_backend,
        provider=cli_provider,
        model=model_name,
    )
    return build_agent_client(
        spec=spec,
        backends=backends,
        skill_source_dirs=skill_source_dirs if skill_source_dirs is not None else [],
        run_log_file=None,
        use_docker=use_docker,
        **kwargs,
    )


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


class TestBuildAgentClient:
    """Tests for :func:`build_agent_client`."""

    def test_build_agent_client_default_is_cli(self):  # noqa: ANN201  # tracked: #288
        runner = _build_client(
            _agent_config(),
            backends={
                "implementer": MagicMock(),
                "judge": MagicMock(),
                "perf_eval": MagicMock(),
            },
        )
        assert runner.backend_name == "cli"
        assert runner.provider == "codex"

    def test_build_agent_client_cli_provider_from_config(self):  # noqa: ANN201  # tracked: #288
        runner = _build_client(_agent_config(backend="cli", cli_provider="claude"))
        assert runner.backend_name == "cli"
        assert runner.provider == "claude"

    def test_build_agent_client_cli_defaults_to_codex(self):  # noqa: ANN201  # tracked: #288
        """When backend=cli and no provider specified, defaults to codex."""
        runner = _build_client(_agent_config(backend="cli"))
        assert runner.backend_name == "cli"
        assert runner.provider == "codex"

    def test_build_agent_client_cli_docker_returns_a_containerized_driver(self):  # noqa: ANN201  # tracked: #288
        """cli backend + docker returns an AgentClient over a containerized AgentShim driver."""
        mock_backends = {
            "implementer": MagicMock(),
            "judge": MagicMock(),
            "perf_eval": MagicMock(),
        }
        runner = _build_client(
            _agent_config(),
            agent_backend="cli",
            cli_provider="claude",
            backends=mock_backends,
            use_docker=True,
        )
        assert isinstance(runner, AgentClient)
        assert isinstance(runner._driver, AgentShimDriver)  # noqa: SLF001
        assert runner._driver._docker_sandboxes is mock_backends  # noqa: SLF001

    def test_build_agent_client_rejects_unsupported_docker_provider(self):  # noqa: ANN201  # tracked: #288
        """A provider unknown to agentshim is rejected building the spec.

        Previously this was only enforced for ``--docker``
        (``DOCKER_PROVIDER_ENV``). ``AgentSpec`` now validates every provider
        against ``agent_catalog()``, which agentshim resolves from the same
        shipped-provider list ``DOCKER_PROVIDER_ENV`` is built from, so any
        provider that clears this check already has a docker env entry.
        """
        with pytest.raises(ValueError, match="nonexistent"):
            _build_client(
                _agent_config(),
                agent_backend="cli",
                cli_provider="nonexistent",
                backends={},
                use_docker=True,
            )

    def test_build_agent_client_rejects_unknown_backend(self):  # noqa: ANN201  # tracked: #288
        """An ``AgentSpec`` rejects a backend string outside {cli, stub} at construction."""
        with pytest.raises(ValueError, match="bogus"):
            agent_spec_from_config(_agent_config(), backend="bogus")

    def test_required_project_enforcement_rejects_non_cli_backend(self):  # noqa: ANN201  # tracked: #288
        """An unsupported ``[agent].backend`` value is rejected building the spec.

        Previously this was caught later, inside ``build_agent_client``, and
        only when ``require_host_sandbox`` was set. ``AgentBackend`` now has
        exactly two members (cli, stub), so an unsupported backend string is
        rejected unconditionally, as soon as an ``AgentSpec`` is resolved.
        """
        with pytest.raises(ValueError, match="unsupported"):
            agent_spec_from_config(_agent_config(backend="unsupported"))

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

        runner = _build_client(config, require_host_sandbox=True)

        assert isinstance(runner, AgentClient)
        assert type(runner._driver).__name__ == "OmnigentDriver"  # noqa: SLF001

    def test_required_project_enforcement_permits_stub(self):  # noqa: ANN201  # tracked: #288
        runner = _build_client(_agent_config(backend="stub"), require_host_sandbox=True)

        assert runner.backend_name == "stub"

    def test_build_agent_client_forwards_project_policy_to_cli(self):  # noqa: ANN201  # tracked: #288
        policy = ProjectPathPolicy(
            read_only_paths=(".state",),
            hidden_paths=(".state/local",),
        )

        runner = _build_client(
            _agent_config(backend="cli", cli_provider="codex"),
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
        return _build_client(config, model_name=model_name)

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
        return _build_client(
            config, agent_backend=agent_backend, cli_provider=cli_provider, model_name=""
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
        runner = self._build(_agent_config(backend="stub"), agent_backend="cli")
        assert isinstance(runner, AgentClient)

    def test_config_can_select_cli_provider(self):  # noqa: ANN201  # tracked: #288
        runner = self._build(_agent_config(backend="cli", cli_provider="claude"))
        assert isinstance(runner, AgentClient)
        assert runner.provider == "claude"
