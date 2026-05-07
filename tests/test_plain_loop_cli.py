"""Tests for the issue outer-loop CLI parser and main()."""

from unittest.mock import patch

import pytest

from vibeserve_agent.cli import _build_plain_parser as build_parser, main
from vibeserve_agent.loops.plain.loop import PlainLoopState


class TestBuildParser:
    def test_default_max_rounds(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.max_rounds == 5

    def test_default_max_attempts_per_issue(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.max_attempts_per_issue == 3

    def test_default_max_issues_per_perf_eval(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.max_issues_per_perf_eval == 3

    def test_default_resume_is_none(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.resume is None

    def test_resume_without_value_defaults_to_latest(self):
        parser = build_parser()
        args = parser.parse_args(["--resume"])
        assert args.resume == "latest"

    def test_resume_with_explicit_dir(self):
        parser = build_parser()
        args = parser.parse_args(["--resume", "20260408-090000-test"])
        assert args.resume == "20260408-090000-test"

    def test_overrides_for_rounds(self):
        parser = build_parser()
        args = parser.parse_args(
            ["--max-rounds", "10",
             "--max-attempts-per-issue", "5",
             "--max-issues-per-perf-eval", "2"]
        )
        assert args.max_rounds == 10
        assert args.max_attempts_per_issue == 5
        assert args.max_issues_per_perf_eval == 2

    def test_common_args_present(self):
        parser = build_parser()
        args = parser.parse_args(["--exp-name", "myexp"])
        assert args.exp_name == "myexp"
        # _add_common_args provides --ref with a default
        assert hasattr(args, "ref")
        assert hasattr(args, "docker")
        assert hasattr(args, "debug")


class TestMain:
    _BASE_ARGV = ["vibe-serve", "--outer-loop", "plain"]

    def _patch_run(self, return_value: bool):
        return patch(
            "vibeserve_agent.loops.plain.loop.run_plain_loop",
            return_value=return_value,
        )

    def _patch_config(self):
        from vibeserve_agent.constants import DEFAULT_COMPUTE_BACKEND

        return patch(
            "vibeserve_agent.cli.load_config_and_skills",
            return_value=(
                {"model": {"name": "claude-sonnet-4-6"}},
                None,
                DEFAULT_COMPUTE_BACKEND,
            ),
        )

    def test_main_exits_zero_on_success(self):
        with patch("sys.argv", list(self._BASE_ARGV)):
            with self._patch_config(), self._patch_run(True):
                main()

    def test_main_exits_one_on_failure(self):
        with patch("sys.argv", list(self._BASE_ARGV)):
            with self._patch_config(), self._patch_run(False):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 1

    def test_main_passes_round_args_to_run_loop(self):
        with patch("sys.argv", [
            *self._BASE_ARGV,
            "--max-rounds", "7",
            "--max-attempts-per-issue", "4",
            "--max-issues-per-perf-eval", "2",
        ]):
            with self._patch_config(), patch(
                "vibeserve_agent.loops.plain.loop.run_plain_loop",
                return_value=True,
            ) as mock_run:
                main()
                kwargs = mock_run.call_args.kwargs
                assert kwargs["max_rounds"] == 7
                assert kwargs["max_attempts_per_issue"] == 4
                assert kwargs["max_issues_per_perf_eval"] == 2

    def test_main_start_round_overrides_loaded_state(self, tmp_path):
        with patch("sys.argv", [
            *self._BASE_ARGV,
            "--resume", "fake-run-dir",
            "--start-round", "3",
        ]):
            with self._patch_config(), patch(
                "vibeserve_agent.cli._resolve_run_dir",
                return_value="fake-run-dir",
            ), patch(
                "vibeserve_agent.loops.plain.loop.run_plain_loop",
                return_value=True,
            ) as mock_run:
                main()
                kwargs = mock_run.call_args.kwargs
                assert kwargs["existing"] is True
                state = kwargs["resume_state"]
                assert isinstance(state, PlainLoopState)
                assert state.round_idx == 2  # 0-indexed
                assert state.bootstrap_done is True

    def test_main_forwards_agent_backend_and_cli_provider(self):
        with patch("sys.argv", [
            *self._BASE_ARGV,
            "--agent-backend", "cli",
            "--cli-provider", "claude",
        ]):
            with self._patch_config(), patch(
                "vibeserve_agent.loops.plain.loop.run_plain_loop",
                return_value=True,
            ) as mock_run:
                main()
                kwargs = mock_run.call_args.kwargs
                assert kwargs["agent_backend"] == "cli"
                assert kwargs["cli_provider"] == "claude"

    def test_main_defaults_agent_backend_and_cli_provider_to_none(self):
        with patch("sys.argv", list(self._BASE_ARGV)):
            with self._patch_config(), patch(
                "vibeserve_agent.loops.plain.loop.run_plain_loop",
                return_value=True,
            ) as mock_run:
                main()
                kwargs = mock_run.call_args.kwargs
                assert kwargs["agent_backend"] is None
                assert kwargs["cli_provider"] is None

    @pytest.mark.parametrize(
        "provider", ["claude", "gemini", "codex", "opencode"]
    )
    def test_main_accepts_all_cli_providers(self, provider):
        """All four CLI providers must reach run_plain_loop without raising."""
        with patch("sys.argv", [
            *self._BASE_ARGV,
            "--agent-backend", "cli",
            "--cli-provider", provider,
        ]):
            with self._patch_config(), patch(
                "vibeserve_agent.loops.plain.loop.run_plain_loop",
                return_value=True,
            ) as mock_run:
                main()
                kwargs = mock_run.call_args.kwargs
                assert kwargs["agent_backend"] == "cli"
                assert kwargs["cli_provider"] == provider
