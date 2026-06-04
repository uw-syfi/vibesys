"""Tests for the :mod:`vibe_serve.agents` runner abstraction."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from vibe_serve.agents import build_agent_runner
from vibe_serve.agents.cli_runner import CliAgentRunner
from vibe_serve.agents.deepagents_runner import DeepAgentsRunner
from vibe_serve.agents.callbacks import AgentLogger
from vibe_serve.config import Config
from vibe_serve.schemas import (
    JudgeResponse,
    Verdict,
)


def _agent_config(**agent) -> Config:
    """Minimal valid Config carrying just an ``[agent]`` section for runner tests."""
    return Config.model_validate({"model": {"name": "m"}, "agent": agent})


def _judge_fallback() -> JudgeResponse:
    return JudgeResponse(
        analysis="fallback",
        feedback="fallback-feedback",
        verdict=Verdict.FAIL,
    )


class TestDeepAgentsRunner:
    """Tests for :class:`DeepAgentsRunner`."""

    def test_deepagents_runner_invoke_returns_structured_response(self, tmp_path):
        pass_response = JudgeResponse(
            analysis="looks good",
            feedback="",
            verdict=Verdict.PASS,
        )
        with patch(
            "vibe_serve.agents.deepagents_runner.create_deep_agent"
        ) as mock_create, patch(
            "vibe_serve.agents.deepagents_runner._run_typed_agent"
        ) as mock_run:
            mock_create.return_value = MagicMock(name="deep_agent")
            mock_run.return_value = pass_response

            runner = DeepAgentsRunner(
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
            )

        assert result is pass_response
        assert mock_run.call_count == 1
        _, kwargs = mock_run.call_args
        assert kwargs["response_cls"] is JudgeResponse
        assert kwargs["fallback_factory"] is _judge_fallback

    def test_deepagents_runner_picks_backend_by_kind(self, tmp_path):
        impl_backend = MagicMock(name="impl-backend")
        judge_backend = MagicMock(name="judge-backend")
        perf_backend = MagicMock(name="perf-backend")

        captured_backends: list = []

        def _capture(**kwargs):
            captured_backends.append(kwargs["backend"])
            return MagicMock(name="deep_agent")

        with patch(
            "vibe_serve.agents.deepagents_runner.create_deep_agent",
            side_effect=_capture,
        ), patch(
            "vibe_serve.agents.deepagents_runner._run_typed_agent",
            return_value=_judge_fallback(),
        ):
            runner = DeepAgentsRunner(
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


# ---------------------------------------------------------------------------
# Helpers for CLI runner tests
# ---------------------------------------------------------------------------


def _make_fake_agent_class(
    *,
    generate_returns: str,
    captured: list,
    generate_raises: type[BaseException] | None = None,
    session_state: dict | None = None,
):
    """Build a fake provider class that records its instances and constructor args.

    When ``session_state`` is provided, ``generate()`` populates
    ``self._last_session`` with a SimpleNamespace carrying ``final_usage``,
    ``total_cost_usd``, and ``duration_ms`` fields — matching the shape
    :class:`CliAgentRunner` reads off ``ClaudeGenerationSession`` after
    ``generate()`` returns.
    """

    from types import SimpleNamespace

    class FakeAgent:
        def __init__(self, model=None, event_handler=None):
            self.model = model
            self.event_handler = event_handler
            self.env: dict[str, str] = {}
            self.generate_calls: list[dict] = []
            self.install_calls: list[dict] = []
            self.uninstall_calls: list[dict] = []
            self.event_log: list[str] = []
            self._last_session: SimpleNamespace | None = None
            captured.append(self)

        def install_mcp_servers(self, workspace, servers):
            self.install_calls.append({"workspace": workspace, "servers": list(servers)})
            self.event_log.append("install")

        def uninstall_mcp_servers(self, workspace, servers):
            self.uninstall_calls.append({"workspace": workspace, "servers": list(servers)})
            self.event_log.append("uninstall")

        def generate(self, prompt, cwd=None, timeout=300, silent=False):
            self.generate_calls.append(
                {
                    "prompt": prompt,
                    "cwd": cwd,
                    "timeout": timeout,
                    "silent": silent,
                }
            )
            self.event_log.append("generate")
            # Mirror :class:`CLICodingAgent.generate`: stash _last_session
            # before invoking the underlying session, so callers can read
            # final state even when the run raises.
            if session_state is not None:
                self._last_session = SimpleNamespace(**session_state)
            if generate_raises is not None:
                raise generate_raises("boom")
            return generate_returns

    return FakeAgent


class TestCliAgentRunner:
    """Tests for :class:`CliAgentRunner`."""

    @pytest.mark.parametrize(
        "provider", ["claude", "gemini", "codex", "opencode"]
    )
    def test_cli_runner_invokes_provider_and_returns_parsed_response(
        self, monkeypatch, tmp_path, provider
    ):
        captured: list = []
        fake_cls = _make_fake_agent_class(
            generate_returns='{"analysis": "ok", "feedback": "", "verdict": "pass"}',
            captured=captured,
        )
        monkeypatch.setitem(
            __import__(
                "vibe_serve.agents.cli_runner",
                fromlist=["_PROVIDER_CLASSES"],
            )._PROVIDER_CLASSES,
            provider,
            fake_cls,
        )

        runner = CliAgentRunner(
            provider=provider,
            model="m",
            run_log_file=None,
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()

        result = runner.invoke(
            kind="judge",
            workspace=workspace,
            system_prompt="sys",
            user_prompt="usr",
            response_cls=JudgeResponse,
            fallback_factory=_judge_fallback,
            round_label="judge #1",
        )

        assert isinstance(result, JudgeResponse)
        assert result.verdict == Verdict.PASS
        assert len(captured) == 1
        assert captured[0].generate_calls[0]["cwd"] == str(workspace)

    def test_cli_runner_falls_back_on_unparseable_output(
        self, monkeypatch, tmp_path
    ):
        captured: list = []
        fake_cls = _make_fake_agent_class(
            generate_returns="banana",
            captured=captured,
        )
        monkeypatch.setitem(
            __import__(
                "vibe_serve.agents.cli_runner",
                fromlist=["_PROVIDER_CLASSES"],
            )._PROVIDER_CLASSES,
            "claude",
            fake_cls,
        )

        runner = CliAgentRunner(
            provider="claude",
            model="m",
            run_log_file=None,
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()

        result = runner.invoke(
            kind="judge",
            workspace=workspace,
            system_prompt="sys",
            user_prompt="usr",
            response_cls=JudgeResponse,
            fallback_factory=_judge_fallback,
            round_label="judge #1",
        )

        assert isinstance(result, JudgeResponse)
        assert result.verdict == Verdict.FAIL
        assert result.feedback == "fallback-feedback"
        assert result.analysis == "fallback"

    def test_cli_runner_parses_fenced_json(self, monkeypatch, tmp_path):
        captured: list = []
        fenced = (
            '```json\n'
            '{"analysis": "fenced", "feedback": "", "verdict": "pass"}\n'
            '```'
        )
        fake_cls = _make_fake_agent_class(
            generate_returns=fenced,
            captured=captured,
        )
        monkeypatch.setitem(
            __import__(
                "vibe_serve.agents.cli_runner",
                fromlist=["_PROVIDER_CLASSES"],
            )._PROVIDER_CLASSES,
            "claude",
            fake_cls,
        )

        runner = CliAgentRunner(
            provider="claude",
            model="m",
            run_log_file=None,
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()

        result = runner.invoke(
            kind="judge",
            workspace=workspace,
            system_prompt="sys",
            user_prompt="usr",
            response_cls=JudgeResponse,
            fallback_factory=_judge_fallback,
            round_label="judge #1",
        )

        assert result.verdict == Verdict.PASS
        assert result.analysis == "fenced"

    def test_cli_runner_materializes_skills_into_workspace(
        self, monkeypatch, tmp_path
    ):
        # Tier-organized source tree (like vibe-serve-skills):
        #   skill_src/
        #     algorithms/myskill/SKILL.md
        #     algorithms/myskill/file.txt
        #     tooling/tool-skill/SKILL.md
        skill_src = tmp_path / "skill_src"
        algo_skill = skill_src / "algorithms" / "myskill"
        algo_skill.mkdir(parents=True)
        (algo_skill / "SKILL.md").write_text("# myskill\n")
        (algo_skill / "file.txt").write_text("hello skill")
        tool_skill = skill_src / "tooling" / "tool-skill"
        tool_skill.mkdir(parents=True)
        (tool_skill / "SKILL.md").write_text("# tool-skill\n")

        captured: list = []
        fake_cls = _make_fake_agent_class(
            generate_returns='{"analysis": "ok", "feedback": "", "verdict": "pass"}',
            captured=captured,
        )
        monkeypatch.setitem(
            __import__(
                "vibe_serve.agents.cli_runner",
                fromlist=["_PROVIDER_CLASSES"],
            )._PROVIDER_CLASSES,
            "claude",
            fake_cls,
        )

        runner = CliAgentRunner(
            provider="claude",
            model="m",
            skills=[skill_src],
            run_log_file=None,
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()

        runner.invoke(
            kind="judge",
            workspace=workspace,
            system_prompt="sys",
            user_prompt="usr",
            response_cls=JudgeResponse,
            fallback_factory=_judge_fallback,
            round_label="judge #1",
        )

        # Each skill is flattened into every per-CLI discovery path,
        # matching the upstream vibe-serve-skills install.sh convention.
        for cli_dir in (
            ".claude/skills",
            ".agents/skills",
            ".gemini/skills",
            ".cursor/skills",
            ".opencode/skills",
        ):
            assert (workspace / cli_dir / "myskill" / "SKILL.md").exists()
            assert (workspace / cli_dir / "myskill" / "file.txt").read_text() == "hello skill"
            assert (workspace / cli_dir / "tool-skill" / "SKILL.md").exists()

    def test_cli_runner_materializes_single_skill_with_nested_content(
        self, monkeypatch, tmp_path
    ):
        # Single-skill source (SKILL.md at the root, sub-dirs are reference
        # material inside the one skill). This mirrors the repo's
        # `serving-systems/` layout.
        skill_src = tmp_path / "serving-systems"
        skill_src.mkdir()
        (skill_src / "SKILL.md").write_text("# serving-systems\n")
        sub = skill_src / "algorithms" / "paged-attention"
        sub.mkdir(parents=True)
        (sub / "SKILL.md").write_text("# paged-attention\n")

        fake_cls = _make_fake_agent_class(
            generate_returns='{"analysis": "ok", "feedback": "", "verdict": "pass"}',
            captured=[],
        )
        monkeypatch.setitem(
            __import__(
                "vibe_serve.agents.cli_runner",
                fromlist=["_PROVIDER_CLASSES"],
            )._PROVIDER_CLASSES,
            "claude",
            fake_cls,
        )

        runner = CliAgentRunner(
            provider="claude",
            model="m",
            skills=[skill_src],
            run_log_file=None,
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()
        runner.invoke(
            kind="judge",
            workspace=workspace,
            system_prompt="sys",
            user_prompt="usr",
            response_cls=JudgeResponse,
            fallback_factory=_judge_fallback,
            round_label="judge #1",
        )

        # Root SKILL.md at top level, nested reference SKILL.md preserved.
        for cli_dir in (".claude/skills", ".agents/skills", ".gemini/skills"):
            assert (workspace / cli_dir / "serving-systems" / "SKILL.md").exists()
            assert (
                workspace / cli_dir / "serving-systems"
                / "algorithms" / "paged-attention" / "SKILL.md"
            ).exists()

    def test_cli_runner_appends_json_schema_to_prompt(
        self, monkeypatch, tmp_path
    ):
        captured: list = []
        fake_cls = _make_fake_agent_class(
            generate_returns='{"analysis": "ok", "feedback": "", "verdict": "pass"}',
            captured=captured,
        )
        monkeypatch.setitem(
            __import__(
                "vibe_serve.agents.cli_runner",
                fromlist=["_PROVIDER_CLASSES"],
            )._PROVIDER_CLASSES,
            "claude",
            fake_cls,
        )

        runner = CliAgentRunner(
            provider="claude",
            model="m",
            run_log_file=None,
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()

        runner.invoke(
            kind="judge",
            workspace=workspace,
            system_prompt="THE-SYSTEM-PROMPT",
            user_prompt="usr",
            response_cls=JudgeResponse,
            fallback_factory=_judge_fallback,
            round_label="judge #1",
        )

        assert len(captured) == 1
        prompt = captured[0].generate_calls[0]["prompt"]
        assert "JudgeResponse" in prompt
        assert prompt.startswith("THE-SYSTEM-PROMPT")

    def test_cli_runner_writes_usage_jsonl_on_success(
        self, monkeypatch, tmp_path
    ):
        """CliAgentRunner appends one JSON record per invoke() to ``<log_dir>/usage.jsonl``."""
        captured: list = []
        fake_cls = _make_fake_agent_class(
            generate_returns='{"analysis": "ok", "feedback": "", "verdict": "pass"}',
            captured=captured,
            session_state={
                "final_usage": {
                    "input_tokens": 14_000,
                    "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 50,
                    "output_tokens": 420,
                },
                "total_cost_usd": 0.0812,
                "duration_ms": 18_431,
            },
        )
        monkeypatch.setitem(
            __import__(
                "vibe_serve.agents.cli_runner",
                fromlist=["_PROVIDER_CLASSES"],
            )._PROVIDER_CLASSES,
            "claude",
            fake_cls,
        )

        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        runner = CliAgentRunner(
            provider="claude",
            model="claude-sonnet-4-6",
            model_name="claude-sonnet-4-6",
            run_log_file=None,
            log_dir=log_dir,
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()

        runner.invoke(
            kind="judge",
            workspace=workspace,
            system_prompt="sys",
            user_prompt="usr",
            response_cls=JudgeResponse,
            fallback_factory=_judge_fallback,
            round_label="judge #1",
        )

        usage_path = log_dir / "usage.jsonl"
        assert usage_path.exists()
        lines = usage_path.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["kind"] == "judge"
        assert record["round_label"] == "judge #1"
        assert record["provider"] == "claude"
        assert record["model"] == "claude-sonnet-4-6"
        assert record["input_tokens"] == 14_000
        assert record["cache_creation_input_tokens"] == 200
        assert record["cache_read_input_tokens"] == 50
        assert record["output_tokens"] == 420
        assert record["total_cost_usd"] == 0.0812
        assert record["duration_ms"] == 18_431
        assert "timestamp" in record

    def test_cli_runner_usage_jsonl_appends_across_invocations(
        self, monkeypatch, tmp_path
    ):
        captured: list = []
        fake_cls = _make_fake_agent_class(
            generate_returns='{"analysis": "ok", "feedback": "", "verdict": "pass"}',
            captured=captured,
            session_state={
                "final_usage": {"input_tokens": 1_000, "output_tokens": 10},
                "total_cost_usd": 0.001,
                "duration_ms": 500,
            },
        )
        monkeypatch.setitem(
            __import__(
                "vibe_serve.agents.cli_runner",
                fromlist=["_PROVIDER_CLASSES"],
            )._PROVIDER_CLASSES,
            "claude",
            fake_cls,
        )

        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        runner = CliAgentRunner(
            provider="claude",
            model="m",
            run_log_file=None,
            log_dir=log_dir,
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()

        for i in range(3):
            runner.invoke(
                kind="implementer",
                workspace=workspace,
                system_prompt="sys",
                user_prompt="usr",
                response_cls=JudgeResponse,
                fallback_factory=_judge_fallback,
                round_label=f"round #{i}",
            )

        lines = (log_dir / "usage.jsonl").read_text().strip().splitlines()
        assert len(lines) == 3
        labels = [json.loads(line)["round_label"] for line in lines]
        assert labels == ["round #0", "round #1", "round #2"]

    def test_cli_runner_usage_jsonl_written_on_parse_failure(
        self, monkeypatch, tmp_path
    ):
        """Even when the CLI returns unparseable output, the tokens were spent —
        the usage record must still be appended so the audit log is complete."""
        captured: list = []
        fake_cls = _make_fake_agent_class(
            generate_returns="not-json-at-all",
            captured=captured,
            session_state={
                "final_usage": {"input_tokens": 7_000, "output_tokens": 100},
                "total_cost_usd": 0.0034,
                "duration_ms": 9_876,
            },
        )
        monkeypatch.setitem(
            __import__(
                "vibe_serve.agents.cli_runner",
                fromlist=["_PROVIDER_CLASSES"],
            )._PROVIDER_CLASSES,
            "claude",
            fake_cls,
        )

        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        runner = CliAgentRunner(
            provider="claude",
            model="m",
            run_log_file=None,
            log_dir=log_dir,
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()

        result = runner.invoke(
            kind="judge",
            workspace=workspace,
            system_prompt="sys",
            user_prompt="usr",
            response_cls=JudgeResponse,
            fallback_factory=_judge_fallback,
            round_label="judge #1",
        )

        # Fallback path fires …
        assert result.verdict == Verdict.FAIL
        # … and usage.jsonl still contains the record.
        usage_path = log_dir / "usage.jsonl"
        assert usage_path.exists()
        record = json.loads(usage_path.read_text().strip().splitlines()[0])
        assert record["input_tokens"] == 7_000
        assert record["total_cost_usd"] == 0.0034

    def test_cli_runner_usage_jsonl_noop_when_log_dir_none(
        self, monkeypatch, tmp_path
    ):
        """Runners built without log_dir (tests, legacy callers) must still succeed."""
        captured: list = []
        fake_cls = _make_fake_agent_class(
            generate_returns='{"analysis": "ok", "feedback": "", "verdict": "pass"}',
            captured=captured,
            session_state={
                "final_usage": {"input_tokens": 5_000},
                "total_cost_usd": 0.002,
                "duration_ms": 1_234,
            },
        )
        monkeypatch.setitem(
            __import__(
                "vibe_serve.agents.cli_runner",
                fromlist=["_PROVIDER_CLASSES"],
            )._PROVIDER_CLASSES,
            "claude",
            fake_cls,
        )

        runner = CliAgentRunner(
            provider="claude",
            model="m",
            run_log_file=None,
            log_dir=None,
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()

        result = runner.invoke(
            kind="judge",
            workspace=workspace,
            system_prompt="sys",
            user_prompt="usr",
            response_cls=JudgeResponse,
            fallback_factory=_judge_fallback,
            round_label="judge #1",
        )
        assert result.verdict == Verdict.PASS

    def test_cli_runner_layers_env_into_subprocess_env(
        self, monkeypatch, tmp_path
    ):
        captured: list = []
        fake_cls = _make_fake_agent_class(
            generate_returns='{"analysis": "ok", "feedback": "", "verdict": "pass"}',
            captured=captured,
        )
        monkeypatch.setitem(
            __import__(
                "vibe_serve.agents.cli_runner",
                fromlist=["_PROVIDER_CLASSES"],
            )._PROVIDER_CLASSES,
            "claude",
            fake_cls,
        )

        runner = CliAgentRunner(
            provider="claude",
            model="m",
            run_log_file=None,
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()

        runner.invoke(
            kind="judge",
            workspace=workspace,
            system_prompt="sys",
            env={"CUDA_VISIBLE_DEVICES": "2"},
            user_prompt="usr",
            response_cls=JudgeResponse,
            fallback_factory=_judge_fallback,
            round_label="judge #1",
        )

        assert len(captured) == 1
        assert captured[0].env.get("CUDA_VISIBLE_DEVICES") == "2"

    def test_cli_runner_docker_uses_command_executor(
        self, monkeypatch, tmp_path
    ):
        from types import SimpleNamespace

        from vibe_serve.agents.docker_executor import DockerCommandExecutor

        captured: list = []

        class FakeAgent:
            def __init__(self, model=None, event_handler=None, executor=None):
                self.model = model
                self.event_handler = event_handler
                self.executor = executor
                self.env: dict[str, str] = {}
                self.generate_calls: list[dict] = []
                self._last_session = SimpleNamespace()
                captured.append(self)

            def install_mcp_servers(self, workspace, servers):
                return None

            def uninstall_mcp_servers(self, workspace, servers):
                return None

            def generate(self, prompt, cwd=None, timeout=300, silent=False):
                self.generate_calls.append(
                    {
                        "prompt": prompt,
                        "cwd": cwd,
                        "timeout": timeout,
                        "silent": silent,
                    }
                )
                return '{"analysis": "ok", "feedback": "", "verdict": "pass"}'

        monkeypatch.setitem(
            __import__(
                "vibe_serve.agents.cli_runner",
                fromlist=["_PROVIDER_CLASSES"],
            )._PROVIDER_CLASSES,
            "claude",
            FakeAgent,
        )

        sandbox = SimpleNamespace(_container_id="container-one")
        runner = CliAgentRunner(
            provider="claude",
            model="m",
            run_log_file=None,
            docker_sandboxes={"judge": sandbox},
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()

        runner.invoke(
            kind="judge",
            workspace=workspace,
            system_prompt="sys",
            user_prompt="usr",
            response_cls=JudgeResponse,
            fallback_factory=_judge_fallback,
            round_label="judge #1",
        )

        assert isinstance(captured[0].executor, DockerCommandExecutor)
        assert captured[0].executor.container_id == "container-one"
        assert captured[0].generate_calls[0]["cwd"] is None

        sandbox._container_id = "container-two"
        runner.invoke(
            kind="judge",
            workspace=workspace,
            system_prompt="sys",
            user_prompt="usr",
            response_cls=JudgeResponse,
            fallback_factory=_judge_fallback,
            round_label="judge #2",
        )

        assert len(captured) == 1
        assert captured[0].executor.container_id == "container-two"

    def test_cli_runner_invokes_install_then_generate_then_uninstall(
        self, monkeypatch, tmp_path
    ):
        """The mcp_servers kwarg triggers a strict install → generate → uninstall sandwich."""
        from vibe_serve._agent_cli.base import MCPServerSpec

        captured: list = []
        fake_cls = _make_fake_agent_class(
            generate_returns='{"analysis": "ok", "feedback": "", "verdict": "pass"}',
            captured=captured,
        )
        monkeypatch.setitem(
            __import__(
                "vibe_serve.agents.cli_runner",
                fromlist=["_PROVIDER_CLASSES"],
            )._PROVIDER_CLASSES,
            "claude",
            fake_cls,
        )

        runner = CliAgentRunner(provider="claude", model="m", run_log_file=None)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        spec = MCPServerSpec(name="vibeserve-issues", command="python", args=["-m", "x"])

        runner.invoke(
            kind="judge",
            workspace=workspace,
            system_prompt="sys",
            user_prompt="usr",
            response_cls=JudgeResponse,
            fallback_factory=_judge_fallback,
            round_label="judge #1",
            mcp_servers=[spec],
        )

        assert len(captured) == 1
        agent = captured[0]
        # Strict ordering: install before generate before uninstall.
        assert agent.event_log == ["install", "generate", "uninstall"]
        assert agent.install_calls[0]["workspace"] == workspace
        assert agent.install_calls[0]["servers"] == [spec]
        assert agent.uninstall_calls[0]["workspace"] == workspace
        assert agent.uninstall_calls[0]["servers"] == [spec]

    def test_cli_runner_uninstalls_even_when_generate_raises(
        self, monkeypatch, tmp_path
    ):
        """uninstall_mcp_servers must run in finally so a crashing generate
        doesn't leave stale config in the workspace."""
        from vibe_serve._agent_cli.base import MCPServerSpec

        captured: list = []
        fake_cls = _make_fake_agent_class(
            generate_returns="",
            captured=captured,
            generate_raises=RuntimeError,
        )
        monkeypatch.setitem(
            __import__(
                "vibe_serve.agents.cli_runner",
                fromlist=["_PROVIDER_CLASSES"],
            )._PROVIDER_CLASSES,
            "claude",
            fake_cls,
        )

        runner = CliAgentRunner(provider="claude", model="m", run_log_file=None)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        spec = MCPServerSpec(name="vibeserve-issues", command="python", args=["-m", "x"])

        with pytest.raises(RuntimeError, match="boom"):
            runner.invoke(
                kind="judge",
                workspace=workspace,
                system_prompt="sys",
                user_prompt="usr",
                response_cls=JudgeResponse,
                fallback_factory=_judge_fallback,
                round_label="judge #1",
                mcp_servers=[spec],
            )

        agent = captured[0]
        assert agent.event_log == ["install", "generate", "uninstall"]

    def test_cli_runner_skips_install_uninstall_when_no_mcp_servers(
        self, monkeypatch, tmp_path
    ):
        """When mcp_servers is None or omitted, install/uninstall hooks are
        not called at all."""
        captured: list = []
        fake_cls = _make_fake_agent_class(
            generate_returns='{"analysis": "ok", "feedback": "", "verdict": "pass"}',
            captured=captured,
        )
        monkeypatch.setitem(
            __import__(
                "vibe_serve.agents.cli_runner",
                fromlist=["_PROVIDER_CLASSES"],
            )._PROVIDER_CLASSES,
            "claude",
            fake_cls,
        )

        runner = CliAgentRunner(provider="claude", model="m", run_log_file=None)
        workspace = tmp_path / "ws"
        workspace.mkdir()

        runner.invoke(
            kind="judge",
            workspace=workspace,
            system_prompt="sys",
            user_prompt="usr",
            response_cls=JudgeResponse,
            fallback_factory=_judge_fallback,
            round_label="judge #1",
            # mcp_servers omitted
        )

        agent = captured[0]
        assert agent.install_calls == []
        assert agent.uninstall_calls == []
        assert agent.event_log == ["generate"]


class TestBuildAgentRunner:
    """Tests for :func:`build_agent_runner`."""

    def test_build_agent_runner_default_is_cli(self):
        runner = build_agent_runner(
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
        assert runner._provider == "codex"

    def test_build_agent_runner_cli_provider_from_config(self):
        runner = build_agent_runner(
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
        assert runner._provider == "claude"

    def test_build_agent_runner_cli_defaults_to_codex(self):
        """When backend=cli and no provider specified, defaults to codex."""
        runner = build_agent_runner(
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
        assert runner._provider == "codex"

    def test_build_agent_runner_cli_docker_returns_cli_runner(self):
        """cli backend + docker now returns a CliAgentRunner with docker_sandboxes."""
        from unittest.mock import MagicMock

        mock_backends = {
            "implementer": MagicMock(),
            "judge": MagicMock(),
            "perf_eval": MagicMock(),
        }
        runner = build_agent_runner(
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
        assert isinstance(runner, CliAgentRunner)
        assert runner._docker_sandboxes is mock_backends

    def test_build_agent_runner_cli_modal_returns_cli_runner(self):
        """cli backend + --modal should wire modal_sandboxes."""
        from unittest.mock import MagicMock

        mock_backends = {
            "implementer": MagicMock(),
            "judge": MagicMock(),
            "perf_eval": MagicMock(),
        }
        runner = build_agent_runner(
            _agent_config(),
            agent_backend="cli",
            cli_provider="codex",
            backends=mock_backends,
            skills=[],
            skill_source_dirs=[],
            model=None,
            model_name="m",
            run_log_file=None,
            use_docker=False,
            use_modal=True,
        )
        assert isinstance(runner, CliAgentRunner)
        assert runner._modal_sandboxes is mock_backends
        assert runner._docker_sandboxes is None

    def test_build_agent_runner_rejects_unsupported_modal_provider(self):
        with pytest.raises(SystemExit, match="not yet supported with --modal"):
            build_agent_runner(
                _agent_config(),
                agent_backend="cli",
                cli_provider="nonexistent",
                backends={},
                skills=[],
                skill_source_dirs=[],
                model=None,
                model_name="m",
                run_log_file=None,
                use_docker=False,
                use_modal=True,
            )

    def test_build_agent_runner_rejects_unsupported_docker_provider(self):
        with pytest.raises(SystemExit, match="not yet supported with --docker"):
            build_agent_runner(
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

    def test_build_agent_runner_rejects_unknown_backend(self):
        with pytest.raises(SystemExit, match="unknown agent backend"):
            build_agent_runner(
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


class TestAgentLoggerEventHandler:
    """Tests for :class:`AgentLogger` as a CLI event handler."""

    def test_agent_logger_event_handler_methods_drive_formatters(self):
        log_file = MagicMock()
        logger = AgentLogger(
            log_file=log_file,
            model_name="m",
            agent_label="Judge",
        )

        with patch.object(logger, "log_text") as mock_text, patch.object(
            logger, "log_tool_call"
        ) as mock_tool_call, patch.object(
            logger, "log_tool_result"
        ) as mock_tool_result:
            logger.on_thinking("hello")
            logger.on_tool_call("Bash", {"command": "ls"})
            logger.on_tool_result("Bash", stdout="output", exit_code=0)
            logger.on_tool_result("Bash", stderr="boom", exit_code=1)

        mock_text.assert_called_once_with("hello")
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

    def test_agent_logger_event_handler_forwards_usage(self):
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

        assert logger._input_tokens == 12_345
        assert logger._latest_usage == usage


class TestBuildAgentRunnerBackendSelection:
    """``build_agent_runner`` backend resolution.

    The default agent backend is ``"cli"`` (provider ``"codex"``) when neither
    the ``--agent-backend`` flag nor an ``[agent].backend`` config key is set.
    Pinned here so the default cannot silently flip.
    """

    def _build(self, config, *, agent_backend=None, cli_provider=None):
        return build_agent_runner(
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

    def test_default_backend_is_cli_with_empty_config(self):
        runner = self._build(_agent_config())
        assert isinstance(runner, CliAgentRunner)
        assert runner._provider == "codex"

    def test_empty_agent_section_defaults_to_cli(self):
        runner = self._build(_agent_config())
        assert isinstance(runner, CliAgentRunner)
        assert runner._provider == "codex"

    def test_agent_backend_flag_overrides_config(self):
        # An explicit --agent-backend flag wins over [agent].backend.
        runner = self._build(_agent_config(backend="deepagents"), agent_backend="cli")
        assert isinstance(runner, CliAgentRunner)

    def test_config_can_select_cli_provider(self):
        runner = self._build(_agent_config(backend="cli", cli_provider="claude"))
        assert isinstance(runner, CliAgentRunner)
        assert runner._provider == "claude"
