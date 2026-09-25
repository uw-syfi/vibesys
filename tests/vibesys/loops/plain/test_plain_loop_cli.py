"""Tests for the issue outer-loop CLI parser and main()."""

import argparse
from contextlib import AbstractContextManager
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from entrypoints.cli import _build_plain_parser as build_parser
from entrypoints.headless import main
from vibesys.api.contracts import LoopKind, RunResult
from vibesys.constants import (
    DEFAULT_COMPUTE_BACKEND,
)
from vibesys.repository import (
    RepositoryVisibility,
)

TARGET_ARGS = [
    "--input",
    "examples/model-serving/Llama-3-8B",
    "--runs-dir",
    "runs",
]


class TestBuildParser:
    def test_default_max_rounds(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        assert args.max_rounds == 5

    def test_default_max_attempts_per_issue(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        assert args.max_attempts_per_issue == 3

    def test_default_max_issues_per_perf_eval(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        assert args.max_issues_per_perf_eval == 3

    def test_default_resume_is_none(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        assert args.resume is None

    def test_resume_without_value_defaults_to_latest(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--resume"])
        assert args.resume == "latest"

    def test_resume_with_explicit_dir(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--resume", "20260408-090000-test"])
        assert args.resume == "20260408-090000-test"

    def test_overrides_for_rounds(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--max-rounds",
                "10",
                "--max-attempts-per-issue",
                "5",
                "--max-issues-per-perf-eval",
                "2",
            ]
        )
        assert args.max_rounds == 10
        assert args.max_attempts_per_issue == 5
        assert args.max_issues_per_perf_eval == 2

    def test_common_args_present(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--exp-name", "myexp"])
        assert args.exp_name == "myexp"
        assert args.input is None
        assert hasattr(args, "docker")
        assert hasattr(args, "debug")


class TestMain:
    _BASE_ARGV: ClassVar[list[str]] = ["vibesys", "--outer-loop", "plain", "--local", *TARGET_ARGS]

    @staticmethod
    def _result(*, succeeded: bool) -> RunResult:
        return RunResult(run_id="test", loop=LoopKind.PLAIN, succeeded=succeeded)

    def _patch_run(self, *, return_value: bool) -> AbstractContextManager[MagicMock]:
        return patch(
            "entrypoints.cli.loops.headless_run",
            return_value=self._result(succeeded=return_value),
        )

    def _patch_config(self) -> AbstractContextManager[MagicMock]:

        # The real load_config_and_skills normalizes args.repo_visibility from
        # config (headless.py); replicate that here so the built RunRequest has a
        # valid enum instead of None.
        def _fake_load(args: argparse.Namespace, *_args: object, **_kwargs: object) -> object:
            if getattr(args, "repo_visibility", None) is None:
                args.repo_visibility = RepositoryVisibility.PRIVATE
            return (
                {"model": {"name": "claude-sonnet-4-6"}},
                None,
                DEFAULT_COMPUTE_BACKEND,
            )

        return patch(
            "entrypoints.cli.load_config_and_skills",
            side_effect=_fake_load,
        )

    def test_main_exits_zero_on_success(self) -> None:
        with (
            patch("sys.argv", list(self._BASE_ARGV)),
            self._patch_config(),
            self._patch_run(return_value=True),
        ):
            main()

    def test_main_exits_one_on_failure(self) -> None:
        with (
            patch("sys.argv", list(self._BASE_ARGV)),
            self._patch_config(),
            self._patch_run(return_value=False),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1

    def test_main_passes_round_args_to_run_loop(self) -> None:
        with (
            patch(
                "sys.argv",
                [
                    *self._BASE_ARGV,
                    "--max-rounds",
                    "7",
                    "--max-attempts-per-issue",
                    "4",
                    "--max-issues-per-perf-eval",
                    "2",
                ],
            ),
            self._patch_config(),
            patch(
                "entrypoints.cli.loops.headless_run",
                return_value=self._result(succeeded=True),
            ) as mock_run,
        ):
            main()
            request = mock_run.call_args.args[0]
            assert request.max_rounds == 7
            assert request.max_attempts_per_issue == 4
            assert request.max_issues_per_perf_eval == 2
            assert request.runs_dir == Path("runs").resolve()

    def test_main_forwards_agent_backend_and_cli_provider(self) -> None:
        with (
            patch(
                "sys.argv",
                [
                    *self._BASE_ARGV,
                    "--agent-backend",
                    "cli",
                    "--cli-provider",
                    "claude",
                ],
            ),
            self._patch_config(),
            patch(
                "entrypoints.cli.loops.headless_run",
                return_value=self._result(succeeded=True),
            ) as mock_run,
        ):
            main()
            request = mock_run.call_args.args[0]
            assert request.agent_backend == "cli"
            assert request.cli_provider == "claude"

    def test_main_defaults_agent_backend_and_cli_provider_to_none(self) -> None:
        with (
            patch("sys.argv", list(self._BASE_ARGV)),
            self._patch_config(),
            patch(
                "entrypoints.cli.loops.headless_run",
                return_value=self._result(succeeded=True),
            ) as mock_run,
        ):
            main()
            request = mock_run.call_args.args[0]
            assert request.agent_backend is None
            assert request.cli_provider is None

    @pytest.mark.parametrize("provider", ["claude", "gemini", "codex", "opencode"])
    def test_main_accepts_all_cli_providers(self, provider: str) -> None:
        """All four CLI providers must reach run_plain_loop without raising."""
        with (
            patch(
                "sys.argv",
                [
                    *self._BASE_ARGV,
                    "--agent-backend",
                    "cli",
                    "--cli-provider",
                    provider,
                ],
            ),
            self._patch_config(),
            patch(
                "entrypoints.cli.loops.headless_run",
                return_value=self._result(succeeded=True),
            ) as mock_run,
        ):
            main()
            request = mock_run.call_args.args[0]
            assert request.agent_backend == "cli"
            assert request.cli_provider == provider
