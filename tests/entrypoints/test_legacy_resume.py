"""Resuming a run that the legacy dynamic loop created fails early, from both entrypoints.

The core cannot continue a legacy run's state. The failure names the run and the file
that marks it legacy, and it arrives before any host probe, workspace or agent opens.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from entrypoints.cli import build_run_request, parse_cli_invocation
from entrypoints.headless import main as headless_main
from entrypoints.run import run_headless
from entrypoints.server import main as server_main
from launch import default_runs
from vibesys.api import ConfigurationError
from vibesys.orchestration.dynamic import DynamicOptions
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_project.api.testing import run_execution_record

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260811-120000-11111111-dynamic"
_NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)
_CODE = "dynamic_legacy_resume_unsupported"


def _project(root: Path) -> Path:
    root.mkdir()
    (root / "OBJECTIVE.md").write_text("Make the queue faster.\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )
    return root


def _legacy_run(root: Path, *, state: bytes | None) -> Path:
    """Record a dynamic run; `state` is the legacy loop's state file, when it wrote one."""
    project = Project.open(root)
    project.state.create_project(root.name)
    manifest = project.state.new_run_manifest(
        root.name,
        run_id=_RUN_ID,
        branch=f"vibesys-runs/{_RUN_ID}",
        vibesys_version="0.2.0-test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(
            id="dynamic",
            config_version=1,
            options=DynamicOptions(
                interface="service",
                max_rounds=2,
                max_retries_per_round=2,
                judge_every=2,
                official_eval_every=2,
            ).model_dump(mode="json"),
        ),
        trusted_input_baseline="0" * 40,
        now=_NOW,
    )
    project.state.create_run(manifest, make_current=True)
    if state is not None:
        project.state.portable_namespace(_RUN_ID, "dynamic").write_bytes("state.json", state)
    return root


def test_the_cli_request_builder_rejects_a_legacy_run_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _legacy_run(_project(tmp_path / "project"), state=b'{"schema_version": 6}')
    monkeypatch.chdir(root)

    with pytest.raises(ConfigurationError) as raised:
        build_run_request(parse_cli_invocation(["--outer-loop", "dynamic", "--resume", _RUN_ID]))

    diagnostic = raised.value.diagnostic
    assert diagnostic.code == _CODE
    assert diagnostic.stage == "resume_resolution"
    assert _RUN_ID in diagnostic.message
    assert "schema 6" in diagnostic.message


def test_the_headless_entrypoint_exits_with_the_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _legacy_run(_project(tmp_path / "project"), state=b'{"schema_version": 6}')
    monkeypatch.chdir(root)

    with pytest.raises(SystemExit) as raised:
        headless_main(["--outer-loop", "dynamic", "--resume", _RUN_ID])

    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert _RUN_ID in error
    assert "legacy dynamic loop" in error
    assert "Traceback" not in error


def test_the_server_entrypoint_exits_with_the_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _legacy_run(_project(tmp_path / "project"), state=b'{"schema_version": 6}')
    monkeypatch.chdir(root)

    with pytest.raises(SystemExit) as raised:
        server_main(
            [
                "--control-socket",
                str(tmp_path / "control.sock"),
                "--outer-loop",
                "dynamic",
                "--resume",
                _RUN_ID,
            ]
        )

    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert _RUN_ID in error
    assert "legacy dynamic loop" in error


@pytest.mark.parametrize("state", [b"", b"not json", b'{"schema_version": 1}', b"[]"])
def test_any_legacy_state_file_is_rejected_whatever_it_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: bytes
) -> None:
    root = _legacy_run(_project(tmp_path / "project"), state=state)
    monkeypatch.chdir(root)

    with pytest.raises(ConfigurationError) as raised:
        build_run_request(parse_cli_invocation(["--outer-loop", "dynamic", "--resume", _RUN_ID]))

    assert raised.value.diagnostic.code == _CODE


def test_the_session_guards_a_request_that_skipped_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run created legacy after the request was validated is still refused, before any resource."""
    root = _legacy_run(_project(tmp_path / "project"), state=None)
    monkeypatch.chdir(root)
    request = build_run_request(
        parse_cli_invocation(["--outer-loop", "dynamic", "--resume", _RUN_ID])
    )
    Project.open(root).state.portable_namespace(_RUN_ID, "dynamic").write_bytes(
        "state.json", b'{"schema_version": 6}'
    )

    with pytest.raises(ConfigurationError) as raised:
        run_headless(request, default_runs())

    assert raised.value.diagnostic.code == _CODE
    assert _RUN_ID in raised.value.diagnostic.message
