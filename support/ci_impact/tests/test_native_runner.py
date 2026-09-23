"""Selected native checks execute only registered roots."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from support.ci_impact.cli import main
from support.ci_impact.model import ROOT, SelectionError
from support.ci_impact.native_runner import run_native_targets

if TYPE_CHECKING:
    from pathlib import Path


def test_runs_only_selected_manifest_commands() -> None:
    calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def fake_run(command: list[str], cwd: Path, env: dict[str, str]) -> None:
        calls.append((command, cwd, env))

    run_native_targets(
        json.dumps(["resources/evaluators/queue", "resources/evaluators/queue/native_runner"]),
        runner=fake_run,
    )
    assert [(command, cwd.relative_to(ROOT).as_posix()) for command, cwd, _ in calls] == [
        (["go", "test", "-race", "./..."], "resources/evaluators/queue"),
        (["cargo", "fmt", "--", "--check"], "resources/evaluators/queue/native_runner"),
        (
            ["cargo", "clippy", "--locked", "--all-targets", "--", "-D", "warnings"],
            "resources/evaluators/queue/native_runner",
        ),
        (["cargo", "test", "--locked"], "resources/evaluators/queue/native_runner"),
    ]


def test_microservice_requires_managed_candidate_tests() -> None:
    calls: list[dict[str, str]] = []
    run_native_targets(
        '["resources/evaluators/microservice"]',
        runner=lambda _command, _cwd, env: calls.append(env),
    )
    assert len(calls) == 1
    assert calls[0]["VIBESYS_REQUIRE_MANAGED_CANDIDATE_TESTS"] == "1"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("[]", "nonempty JSON array"),
        ('["resources/evaluators/queue", "resources/evaluators/queue"]', "duplicate"),
        ('["resources/evaluators/not-registered"]', "unregistered native targets"),
        ("not-json", "JSON array"),
    ],
)
def test_invalid_targets_fail_before_running(raw: str, message: str) -> None:
    with pytest.raises(SelectionError, match=message):
        run_native_targets(raw, runner=lambda *_args: pytest.fail("unexpected command"))


def test_sdk_module_path_checked_before_go_test(tmp_path: Path) -> None:
    sdk = tmp_path / "sdk/vs-evaluator/vseval"
    sdk.mkdir(parents=True)
    (tmp_path / "ci-components.toml").write_text(
        'native_ci_roots = ["sdk/vs-evaluator/vseval"]\n', encoding="utf-8"
    )
    (sdk / "go.mod").write_text("module wrong/module\n", encoding="utf-8")
    with pytest.raises(SelectionError, match="expected"):
        run_native_targets(
            '["sdk/vs-evaluator/vseval"]',
            root=tmp_path,
            runner=lambda *_args: pytest.fail("unexpected command"),
        )


def test_cli_reports_invalid_native_target(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run-native", "--targets-json", "[]"]) == 2
    assert "nonempty JSON array" in capsys.readouterr().err
