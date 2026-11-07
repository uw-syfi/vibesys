"""How the Modal evaluator drives the Modal CLI.

``modal deploy`` runs as ``uv run modal deploy`` in the candidate's project, so its
standard error also carries uv's notices (``Using CPython 3.12.3 interpreter at: ...``)
and anything the candidate's module writes there. Modal renders the deploy summary on
standard output, wrapping long URLs at the console width; whatever else the run printed,
the evaluator must target exactly the app Modal deployed.

The evaluator runs as one standalone file that reads its runtime directory (its lock and
deployment lease) from ``XDG_RUNTIME_DIR`` when it starts; tests that run it whole load
that file the same way, with a runtime directory of their own.
"""

from __future__ import annotations

import json
import runpy
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_sandbox.api.evaluator_helpers import MODAL_EVALUATOR_HELPER

# test-isolation: the deploy parser lives inside the standalone mounted helper, which is its only import path.
from vs_sandbox.modal_evaluator import (
    _EXEC_RC_MARKER,
    ProgramResult,
    extract_modal_app_identifier,
    extract_modal_web_url,
)
from vs_sim.api.testing import ManualClock

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_UV_PYTHON_NOTICE = "Using CPython 3.12.3 interpreter at: /usr/bin/python3\n"
"""What ``uv run`` prints on standard error when it creates or resolves the project venv."""

_APP = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", fullmatch=True)
"""A Modal app name: letters, digits, dashes, periods and underscores, under 64 characters."""

_NAME = st.from_regex(r"[a-z0-9][a-z0-9-]{0,47}", fullmatch=True)
"""A Modal workspace, environment or web endpoint label."""

_GUIDES = st.sampled_from(["    ", "│   "])
"""The prefix Rich's tree puts before a wrapped line of its last or a middle child."""


@dataclass(frozen=True)
class _Deployment:
    """What one ``modal deploy`` deployed, and the width Modal's console rendered it at."""

    workspace: str
    environment: str
    app: str
    label: str
    width: int
    guide: str

    @property
    def web_url(self) -> str:
        """The web endpoint Modal serves the app's web function at."""
        return f"https://{self.workspace}--{self.label}.modal.run"

    @property
    def stdout(self) -> str:
        """The summary Modal prints on standard output, laid out as Rich lays it out."""
        deployment_url = (
            f"https://modal.com/apps/{self.workspace}/{self.environment}/deployed/{self.app}"
        )
        lines = [
            "✓ Created objects.",
            "├── 🔨 Created mount /workspace/main.py",
            "└── 🔨 Created web function serve => ",
            *_folded(self.web_url, self.width, self.guide),
            "✓ App deployed in 3.217s! 🎉",
            "",
            "View Deployment: ",
            *_folded(deployment_url, self.width, ""),
        ]
        return "\n".join(lines) + "\n"


def _folded(text: str, width: int, indent: str) -> list[str]:
    """``text`` on lines of its own, folded at ``width`` columns after ``indent``.

    Rich moves a word longer than the remaining room to a new line and cuts it into
    pieces of the full width, as Modal's deploy summary shows for a long URL.
    """
    room = width - len(indent)
    return [indent + text[start : start + room] for start in range(0, len(text), room)]


_DEPLOYMENTS = st.builds(
    _Deployment,
    workspace=_NAME,
    environment=_NAME,
    app=_APP,
    label=_NAME,
    width=st.integers(min_value=24, max_value=200),
    guide=_GUIDES,
)
_ISSUE_DEPLOYMENT = _Deployment(
    workspace="workspace",
    environment="main",
    app="vibesys-20261107-150945-cb40aca1-r3-20261107-150945",
    label="vibesys-20261107-150945-cb40aca1-r3-serve",
    width=80,
    guide="    ",
)


@pytest.mark.regression
@given(deployment=_DEPLOYMENTS, before=st.text(), stderr=st.text())
@example(deployment=_ISSUE_DEPLOYMENT, before="", stderr=_UV_PYTHON_NOTICE)
def test_the_app_and_endpoint_are_exactly_those_in_modals_summary(
    deployment: _Deployment, before: str, stderr: str
) -> None:
    """Regression (#4098): uv's notice on standard error was glued onto the app name.

    Output before the summary (image build logs, the candidate module's prints) and
    anything at all on standard error leave both readings unchanged.
    """
    deploy = ProgramResult(0, f"{before}\n{deployment.stdout}", stderr)

    assert extract_modal_app_identifier(deploy) == deployment.app
    assert extract_modal_web_url(deploy) == deployment.web_url


def _standalone_helper(runtime_dir: Path) -> SimpleNamespace:
    """The helper's globals as the run environment starts it, with ``runtime_dir`` its own."""
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
        return SimpleNamespace(
            **runpy.run_path(str(MODAL_EVALUATOR_HELPER), run_name="vibesys_modal_evaluator")
        )


class _AdvancingClock:
    """A manual clock whose blocking sleep moves it forward."""

    def __init__(self) -> None:
        """Start at zero."""
        self._clock = ManualClock(0.0)

    def now(self) -> float:
        """The current time."""
        return self._clock.now()

    def sleep(self, seconds: float) -> None:
        """Pause ``seconds`` manual seconds."""
        self._clock.advance(seconds)


_DEPLOY_BEGAN = datetime(2026, 11, 7, 20, 59, tzinfo=UTC).timestamp()
"""The host's wall clock, in seconds since the epoch, when the first deploy starts."""

_DEPLOY_SECONDS = 43.0
"""How long one ``modal deploy`` takes on that clock; the app's container starts as it ends."""


def _modal_start_time(seconds: float) -> str:
    """``seconds`` since the epoch as ``modal container list --json`` prints a start time."""
    return datetime.fromtimestamp(seconds, UTC).isoformat(sep=" ", timespec="seconds")


class _ModalCli:
    """Answers the Modal CLI the way a workspace with one deployable app does.

    ``wall_clock`` is the host's calendar clock; a deploy takes ``_DEPLOY_SECONDS`` on it,
    and the app's container starts when the deploy ends.
    """

    def __init__(self, deployment: _Deployment, deploy_stderr: str) -> None:
        """Deploy ``deployment``, printing ``deploy_stderr`` beside Modal's summary."""
        self._deployment = deployment
        self._deploy_stderr = deploy_stderr
        self._started = _DEPLOY_BEGAN
        self.wall_clock = ManualClock(_DEPLOY_BEGAN)
        self.calls: list[list[str]] = []

    def run(self, argv: Sequence[str], *, cwd: str, timeout: float | None) -> ProgramResult:
        """Record the call and answer it."""
        del cwd, timeout
        arguments = list(argv[list(argv).index("modal") + 1 :])
        self.calls.append(arguments)
        match arguments[:2]:
            case ["deploy", _]:
                self.wall_clock.advance(_DEPLOY_SECONDS)
                self._started = self.wall_clock.now()
                return ProgramResult(0, self._deployment.stdout, self._deploy_stderr)
            case ["container", "list"]:
                started = _modal_start_time(self._started)
                listing = [
                    {
                        "app_name": self._deployment.app,
                        "container_id": "ta-1",
                        "start_time": started,
                    }
                ]
                return ProgramResult(0, json.dumps(listing), "")
            case ["container", "exec"]:
                return ProgramResult(0, f"{_EXEC_RC_MARKER}0\n", "")
            case ["app", "stop"]:
                return ProgramResult(0, "", "")
            case _:
                raise AssertionError(arguments)


@pytest.mark.regression
@given(deployment=_DEPLOYMENTS, stderr=st.text())
@example(deployment=_ISSUE_DEPLOYMENT, stderr=_UV_PYTHON_NOTICE)
def test_the_evaluator_runs_in_and_stops_the_app_modal_deployed(
    deployment: _Deployment, stderr: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Regression (#4098): a gate failed to find its container and left the app deployed.

    Whatever the deploy printed on standard error, the gate runs in a container of the
    deployed app and the release stops that app.
    """
    helper = _standalone_helper(tmp_path_factory.mktemp("runtime"))
    workspace = tmp_path_factory.mktemp("workspace")
    (workspace / "main.py").write_text("import modal\n")
    modal = _ModalCli(deployment, stderr)
    host = helper.ModalHost(
        clock=_AdvancingClock(),
        wall_clock=modal.wall_clock,
        programs=modal,
        health=lambda _url, _timeout: 200,
        keep_warm=lambda _base_url: nullcontext(),
    )

    with pytest.MonkeyPatch.context() as environment:
        environment.delenv("VIBESYS_CANDIDATE_REVISION", raising=False)
        environment.setenv("VIBESYS_RELEASE_MODAL_DEPLOYMENT", "1")
        status = helper.run_evaluator(["true"], host=host, workspace=str(workspace))

    assert status == 0
    assert [call[:3] for call in modal.calls if call[:2] == ["container", "exec"]] == [
        ["container", "exec", "ta-1"]
    ]
    assert [call for call in modal.calls if call[:2] == ["app", "stop"]] == [
        ["app", "stop", deployment.app, "--yes"]
    ]
