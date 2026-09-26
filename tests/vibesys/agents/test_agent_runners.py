"""Tests for application-level agent clients."""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING, Any, TypedDict, Unpack, cast
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from headless.render import HeadlessRenderer
from vibesys.agent_spec_config import agent_spec_from_config
from vibesys.config import Config
from vibesys.render.log import log_json_and_print, log_prompt_markdown_and_print
from vibesys.roles.common import Verdict
from vibesys.roles.judge import JudgeResponse
from vs_agent.api import AgentClient, build_agent_client
from vs_agent.callbacks import AgentLogger
from vs_sandbox.api import ProjectPathPolicy


def _agent_config(**agent: object) -> Config:
    """Minimal valid Config carrying just an ``[agent]`` section for runner tests."""
    return Config.model_validate({"model": {"name": "m"}, "agent": agent})


class _BuildClientOptions(TypedDict, total=False):
    """Optional client-factory inputs used by these composition tests."""

    backends: dict[str, Any] | None
    skill_source_dirs: list[Path] | None
    use_docker: bool
    require_host_sandbox: bool
    project_path_policy: ProjectPathPolicy | None


def _build_client(
    config: Config,
    *,
    agent_backend: str | None = None,
    cli_provider: str | None = None,
    model_name: str | None = "m",
    **kwargs: Unpack[_BuildClientOptions],
) -> AgentClient:
    """Resolve an :class:`AgentSpec` the way application config does, then build."""
    spec = agent_spec_from_config(
        config,
        backend=agent_backend,
        provider=cli_provider,
        model=model_name,
    )
    return cast(
        "AgentClient",
        build_agent_client(
            spec=spec,
            backends=kwargs.get("backends"),
            skill_source_dirs=kwargs.get("skill_source_dirs") or [],
            run_log_file=None,
            use_docker=kwargs.get("use_docker", False),
            require_host_sandbox=kwargs.get("require_host_sandbox", False),
            project_path_policy=kwargs.get("project_path_policy"),
        ),
    )


def _judge_fallback() -> JudgeResponse:
    return JudgeResponse(
        analysis="fallback",
        feedback="fallback-feedback",
        verdict=Verdict.FAIL,
    )


def test_prompt_markdown_emitter_preserves_raw_log_and_truncates_stdout(
    capsys: pytest.CaptureFixture[str], headless_renderer: HeadlessRenderer
) -> None:
    headless_renderer.max_text_len = 20
    log = StringIO()
    prompt = "# Title\n\nUse **markdown** and `code`."

    log_prompt_markdown_and_print(prompt, log_file=log)

    stdout = capsys.readouterr().out
    assert "# Title" in stdout
    assert "... [17 more chars, see log for full text]" in stdout
    assert log.getvalue() == prompt + "\n"


def test_json_emitter_preserves_raw_log(capsys: pytest.CaptureFixture[str]) -> None:
    log = StringIO()
    raw_json = '{"analysis":"ok","items":[1,2]}'

    log_json_and_print(raw_json, log_file=log)

    stdout = capsys.readouterr().out
    assert raw_json in stdout
    assert log.getvalue() == raw_json + "\n"


class TestBuildAgentClient:
    """Tests for :func:`build_agent_client`."""

    def test_build_agent_client_default_is_cli(self) -> None:
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

    def test_build_agent_client_cli_provider_from_config(self) -> None:
        runner = _build_client(_agent_config(backend="cli", cli_provider="claude"))
        assert runner.backend_name == "cli"
        assert runner.provider == "claude"

    def test_build_agent_client_cli_defaults_to_codex(self) -> None:
        """When backend=cli and no provider specified, defaults to codex."""
        runner = _build_client(_agent_config(backend="cli"))
        assert runner.backend_name == "cli"
        assert runner.provider == "codex"

    def test_build_agent_client_cli_docker_enables_container_execution(self) -> None:
        """cli backend + docker advertises container execution and no host grants."""
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
        assert runner.capabilities.container_execution
        assert not runner.capabilities.host_path_grants

    def test_build_agent_client_rejects_unsupported_docker_provider(self) -> None:
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

    def test_build_agent_client_rejects_unknown_backend(self) -> None:
        """An ``AgentSpec`` rejects a backend string outside {cli, stub} at construction."""
        with pytest.raises(ValueError, match="bogus"):
            agent_spec_from_config(_agent_config(), backend="bogus")

    def test_required_project_enforcement_rejects_non_cli_backend(self) -> None:
        """An unsupported ``[agent].backend`` value is rejected building the spec.

        Previously this was caught later, inside ``build_agent_client``, and
        only when ``require_host_sandbox`` was set. ``AgentBackend`` now has
        exactly two members (cli, stub), so an unsupported backend string is
        rejected unconditionally, as soon as an ``AgentSpec`` is resolved.
        """
        with pytest.raises(ValueError, match="unsupported"):
            agent_spec_from_config(_agent_config(backend="unsupported"))

    def test_required_workspace_enforcement_permits_omnigent(self) -> None:
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
        assert runner.driver_name == "omnigent"

    def test_required_project_enforcement_permits_stub(self) -> None:
        runner = _build_client(_agent_config(backend="stub"), require_host_sandbox=True)

        assert runner.backend_name == "stub"

    def test_build_agent_client_forwards_project_policy_to_cli(self, tmp_path: Path) -> None:
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
        with patch.object(runner, "run", return_value=MagicMock(text="ok")) as run:
            runner.invoke_text(
                kind="implementer",
                workspace=tmp_path,
                system_prompt="instructions",
                user_prompt="prompt",
                round_label="policy wiring",
            )
        session_spec = run.call_args.kwargs["session_spec"]
        assert session_spec.policy.project_paths is policy
        assert session_spec.policy.require_enforcement is True

    # --- model resolution for the cli backend ---------------------------------
    #
    # Regression coverage for the config API where [model].name did not reach
    # the CLI tool. [model].name is the single source of truth: it must be the
    # model handed to the CLI tool, and the displayed model_name must equal it
    # so the run-log header can't report a model that isn't running.

    @staticmethod
    def _cli_client(config: Config, *, model_name: str) -> AgentClient:
        return _build_client(config, model_name=model_name)

    @pytest.mark.parametrize("provider", ["claude", "gemini", "codex", "opencode"])
    def test_cli_backend_uses_model_name(self, provider: str) -> None:
        runner = self._cli_client(
            _agent_config(backend="cli", cli_provider=provider),
            model_name="gpt-5.4",
        )
        assert runner.model_for_kind("implementer") == "gpt-5.4"

    def test_displayed_model_name_matches_model_passed(self) -> None:
        # The effective default model is exposed through the client contract.
        runner = self._cli_client(
            _agent_config(backend="cli", cli_provider="codex"),
            model_name="gpt-5.4",
        )
        assert runner.model_for_kind("implementer") == "gpt-5.4"

    def test_cli_backend_carries_outer_and_inner_role_configuration(self, tmp_path: Path) -> None:
        config = _agent_config(
            backend="cli",
            cli_provider="codex",
            outer={"model": "gpt-5.6-sol", "reasoning_effort": "xhigh"},
            inner={"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"},
        )
        config.thinking.level = "high"
        runner = self._cli_client(config, model_name="gpt-5.6-sol")

        with patch.object(runner, "run", return_value=MagicMock(text="ok")) as run:
            for kind in ("orchestrator", "implementer"):
                runner.invoke_text(
                    kind=kind,
                    workspace=tmp_path,
                    system_prompt="instructions",
                    user_prompt="prompt",
                    round_label="role configuration",
                )
        outer_spec = run.call_args_list[0].kwargs["session_spec"]
        inner_spec = run.call_args_list[1].kwargs["session_spec"]
        assert outer_spec.model == "gpt-5.6-sol"
        assert outer_spec.reasoning_effort == "xhigh"
        assert inner_spec.model == "gpt-5.6-luna"
        assert inner_spec.reasoning_effort == "xhigh"
        assert runner.model_for_kind("judge") == "gpt-5.6-sol"


class TestAgentLoggerEventHandler:
    """Tests for :class:`AgentLogger` as a CLI event handler."""

    def test_agent_logger_event_handler_methods_drive_formatters(self) -> None:
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

    def test_agent_logger_event_handler_forwards_usage(self) -> None:
        event_sink = MagicMock()
        logger = AgentLogger(
            event_sink=event_sink,
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

        event_sink.usage_update.assert_called_once()
        assert event_sink.usage_update.call_args.args == (12_345,)


class TestBuildAgentClientBackendSelection:
    """``build_agent_client`` backend resolution.

    The default agent backend is ``"cli"`` (provider ``"codex"``) when neither
    the ``--agent-backend`` flag nor an ``[agent].backend`` config key is set.
    Pinned here so the default cannot silently flip.
    """

    def _build(
        self, config: Config, *, agent_backend: str | None = None, cli_provider: str | None = None
    ) -> AgentClient:
        return _build_client(
            config, agent_backend=agent_backend, cli_provider=cli_provider, model_name=""
        )

    def test_default_backend_is_cli_with_empty_config(self) -> None:
        runner = self._build(_agent_config())
        assert isinstance(runner, AgentClient)
        assert runner.provider == "codex"

    def test_empty_agent_section_defaults_to_cli(self) -> None:
        runner = self._build(_agent_config())
        assert isinstance(runner, AgentClient)
        assert runner.provider == "codex"

    def test_agent_backend_flag_overrides_config(self) -> None:
        # An explicit --agent-backend flag wins over [agent].backend.
        runner = self._build(_agent_config(backend="stub"), agent_backend="cli")
        assert isinstance(runner, AgentClient)

    def test_config_can_select_cli_provider(self) -> None:
        runner = self._build(_agent_config(backend="cli", cli_provider="claude"))
        assert isinstance(runner, AgentClient)
        assert runner.provider == "claude"
