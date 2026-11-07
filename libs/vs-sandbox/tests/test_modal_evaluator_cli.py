"""How the Modal evaluator drives the Modal CLI.

``modal deploy`` runs as ``uv run modal deploy`` in the candidate's project, so its
standard error also carries uv's notices (``Using CPython 3.12.3 interpreter at: ...``)
and anything the candidate's module writes there. Modal renders the deploy summary on
standard output, wrapping long URLs at the console width; whatever else the run printed,
the evaluator must target exactly the app Modal deployed.

Only that deploy belongs in the candidate's environment, because it imports the
candidate's module. The evaluator's own commands (``app logs``, ``app stop``,
``container list`` and ``container exec``) run on the framework's Modal client, so the
flags they use work whatever Modal the candidate pins, and a stop that fails is retried
by the next evaluation instead of forgotten.

The evaluator runs as one standalone file that reads its runtime directory (its lock and
deployment lease) from ``XDG_RUNTIME_DIR`` when it starts; tests that run it whole load
that file the same way, with a runtime directory of their own.
"""

from __future__ import annotations

import json
import runpy
import shutil
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal

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


_StopEnd = Literal["stopped", "refused", "timed_out"]
"""How one ``modal app stop`` ends: it stops the app, the client rejects ``--yes``, or it hangs."""

_STOP_ENDS = st.sampled_from(["stopped", "refused", "timed_out"])


@dataclass(frozen=True)
class _ModalBehavior:
    """Whether a deployed app's container ever starts, and how each ``app stop`` ends.

    Stops end as ``stop_ends`` lists them, then as ``stopped``.
    """

    containers_start: bool = True
    stop_ends: tuple[_StopEnd, ...] = ()


_UNEVENTFUL = _ModalBehavior()
"""Every container starts and every stop succeeds."""


@dataclass(frozen=True)
class _Call:
    """One Modal CLI run: the program that ran the CLI, and the CLI's arguments."""

    program: tuple[str, ...]
    arguments: tuple[str, ...]


_DEPLOY_BEGAN = datetime(2026, 11, 7, 20, 59, tzinfo=UTC).timestamp()
"""The host's wall clock, in seconds since the epoch, when the first deploy starts."""

_DEPLOY_SECONDS = 43.0
"""How long one ``modal deploy`` takes on that clock; the app's container starts as it ends."""


def _modal_start_time(seconds: float) -> str:
    """``seconds`` since the epoch as ``modal container list --json`` prints a start time."""
    return datetime.fromtimestamp(seconds, UTC).isoformat(sep=" ", timespec="seconds")


class _ModalWorkspace:
    """A Modal workspace behind the CLI, into which the candidate deploys one app.

    It records every run and keeps which apps are deployed; ``behavior`` says whether a
    deployed app's container shows in ``container list`` and how each stop ends.
    ``wall_clock`` is the host's calendar clock: a deploy takes ``_DEPLOY_SECONDS`` on it,
    and the app's new container starts when the deploy ends.
    """

    def __init__(
        self,
        deployment: _Deployment = _ISSUE_DEPLOYMENT,
        *,
        deploy_stderr: str = "",
        behavior: _ModalBehavior = _UNEVENTFUL,
    ) -> None:
        """Deploy ``deployment``, printing ``deploy_stderr`` beside Modal's summary."""
        self._deployment = deployment
        self._deploy_stderr = deploy_stderr
        self._containers_start = behavior.containers_start
        self._stop_ends = iter(behavior.stop_ends)
        self._started: dict[str, float] = {}
        self.wall_clock = ManualClock(_DEPLOY_BEGAN)
        self.deployed: set[str] = set()
        self.calls: list[_Call] = []

    def run(self, argv: Sequence[str], *, cwd: str, timeout: float | None) -> ProgramResult:
        """Record the run and answer it as Modal would."""
        del cwd, timeout
        split = list(argv).index("modal") + 1
        call = _Call(tuple(argv[:split]), tuple(argv[split:]))
        self.calls.append(call)
        match call.arguments:
            case ("deploy", _):
                self.wall_clock.advance(_DEPLOY_SECONDS)
                self.deployed.add(self._deployment.app)
                self._started[self._deployment.app] = self.wall_clock.now()
                return ProgramResult(0, self._deployment.stdout, self._deploy_stderr)
            case ("container", "list", "--json"):
                running = sorted(self.deployed) if self._containers_start else []
                listing = [
                    {
                        "app_name": app,
                        "container_id": f"ta-{app}",
                        "start_time": _modal_start_time(self._started[app]),
                    }
                    for app in running
                ]
                return ProgramResult(0, json.dumps(listing), "")
            case ("container", "exec", *_):
                return ProgramResult(0, f"{_EXEC_RC_MARKER}0\n", "")
            case ("app", "logs", app, *_):
                return ProgramResult(0, f"{app}: RuntimeError: CUDA error: no kernel image\n", "")
            case ("app", "stop", app, "--yes"):
                return self._stop(app)
            case _:
                raise AssertionError(call)

    def _stop(self, app: str) -> ProgramResult:
        end = next(self._stop_ends, "stopped")
        if end == "timed_out":
            message = "modal timed out after 60 seconds"
            raise TimeoutError(message)
        if end == "refused":
            return ProgramResult(2, "", "No such option: --yes (Possible options: --env)")
        if app not in self.deployed:
            return ProgramResult(1, "", f"Could not find a deployed app named '{app}'.")
        self.deployed.remove(app)
        return ProgramResult(0, "", "")


@dataclass(frozen=True)
class _Evaluation:
    """One gate's evaluation: the candidate revision, whether it releases, and readiness."""

    revision: str | None = "rev-1"
    release: bool = False
    healthy: bool = True


def _evaluate(
    helper: SimpleNamespace, modal: _ModalWorkspace, workspace: Path, gate: _Evaluation
) -> int:
    """Run one trusted evaluation of ``workspace`` against ``modal``; return its status."""
    host = helper.ModalHost(
        clock=_AdvancingClock(),
        wall_clock=modal.wall_clock,
        programs=modal,
        health=lambda _url, _timeout: 200 if gate.healthy else 503,
        keep_warm=lambda _base_url: nullcontext(),
    )
    with pytest.MonkeyPatch.context() as environment:
        if gate.revision is None:
            environment.delenv("VIBESYS_CANDIDATE_REVISION", raising=False)
        else:
            environment.setenv("VIBESYS_CANDIDATE_REVISION", gate.revision)
        if gate.release:
            environment.setenv("VIBESYS_RELEASE_MODAL_DEPLOYMENT", "1")
        else:
            environment.delenv("VIBESYS_RELEASE_MODAL_DEPLOYMENT", raising=False)
        return helper.run_evaluator(["true"], host=host, workspace=str(workspace))


def _evaluator(tmp_path_factory: pytest.TempPathFactory) -> tuple[SimpleNamespace, Path]:
    """A freshly started helper with its own runtime directory, and a candidate workspace."""
    helper = _standalone_helper(tmp_path_factory.mktemp("runtime"))
    workspace = tmp_path_factory.mktemp("workspace")
    (workspace / "main.py").write_text("import modal\n")
    return helper, workspace


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
    helper, workspace = _evaluator(tmp_path_factory)
    modal = _ModalWorkspace(deployment, deploy_stderr=stderr)

    status = _evaluate(helper, modal, workspace, _Evaluation(revision=None, release=True))

    assert status == 0
    assert [
        call.arguments[:3] for call in modal.calls if call.arguments[:2] == ("container", "exec")
    ] == [("container", "exec", f"ta-{deployment.app}")]
    assert [call.arguments for call in modal.calls if call.arguments[:2] == ("app", "stop")] == [
        ("app", "stop", deployment.app, "--yes")
    ]
    assert modal.deployed == set()


_PROJECT_MODAL = (shutil.which("uv") or "uv", "run", "--quiet", "modal")
"""The program that runs the candidate project's Modal CLI."""


_EVALUATIONS = st.lists(
    st.builds(
        _Evaluation,
        revision=st.sampled_from([None, "rev-1", "rev-2"]),
        release=st.booleans(),
        healthy=st.booleans(),
    ),
    min_size=1,
    max_size=3,
)


_BEHAVIORS = st.builds(
    _ModalBehavior,
    containers_start=st.booleans(),
    stop_ends=st.lists(_STOP_ENDS, max_size=4).map(tuple),
)


@pytest.mark.regression
@given(evaluations=_EVALUATIONS, behavior=_BEHAVIORS)
@example(evaluations=[_Evaluation(release=True, healthy=False)], behavior=_UNEVENTFUL)
def test_only_the_deploy_runs_on_the_modal_the_candidate_pins(
    evaluations: list[_Evaluation],
    behavior: _ModalBehavior,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Regression (#4041): ``app logs --since`` and ``app stop --yes`` ran on the candidate's pin.

    On every path (deploy, reuse, a readiness or container discovery failure, a release,
    a stop that fails), each Modal command except ``modal deploy`` runs on the
    framework's client, and ``modal deploy`` runs in the candidate's project.
    """
    helper, workspace = _evaluator(tmp_path_factory)
    modal = _ModalWorkspace(behavior=behavior)

    for evaluation in evaluations:
        _evaluate(helper, modal, workspace, evaluation)

    framework = helper.FRAMEWORK_MODAL_CLI
    assert framework[1:] == ("-I", "-m", "modal")
    assert any(call.arguments[0] == "deploy" for call in modal.calls)
    for call in modal.calls:
        assert call.program == (_PROJECT_MODAL if call.arguments[0] == "deploy" else framework)


@pytest.mark.regression
@given(
    failure=st.sampled_from(["refused", "timed_out"]),
    next_revision=st.sampled_from(["rev-1", "rev-2"]),
)
@example(failure="refused", next_revision="rev-2")
def test_an_app_whose_stop_failed_is_stopped_by_the_next_evaluation(
    failure: _StopEnd, next_revision: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Regression (#4041): a failed stop dropped the deployment lease, so nothing retried it.

    The gate's own status does not change. The next evaluation stops the app before it
    deploys, unless the app still serves that evaluation's revision and is reused.
    """
    helper, workspace = _evaluator(tmp_path_factory)
    app = _ISSUE_DEPLOYMENT.app
    modal = _ModalWorkspace(behavior=_ModalBehavior(stop_ends=(failure,)))

    released = _evaluate(helper, modal, workspace, _Evaluation(revision="rev-1", release=True))
    assert released == 0
    assert modal.deployed == {app}
    first_call_of_next = len(modal.calls)
    status = _evaluate(helper, modal, workspace, _Evaluation(revision=next_revision))

    assert status == 0
    next_calls = [call.arguments for call in modal.calls[first_call_of_next:]]
    if next_revision == "rev-1":
        assert all(arguments[0] != "deploy" for arguments in next_calls)
        assert ("container", "exec", f"ta-{app}") in [arguments[:3] for arguments in next_calls]
    else:
        assert next_calls[0] == ("app", "stop", app, "--yes")
        assert next_calls[1][0] == "deploy"


def test_a_container_that_never_starts_shows_the_apps_recent_logs(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression (#4041): a server that crashed at startup left the agent no reason why."""
    helper, workspace = _evaluator(tmp_path_factory)
    app = _ISSUE_DEPLOYMENT.app
    modal = _ModalWorkspace(behavior=_ModalBehavior(containers_start=False))

    status = _evaluate(helper, modal, workspace, _Evaluation(revision=None, release=True))

    assert status == 1
    assert [
        call.arguments[:3] for call in modal.calls if call.arguments[:2] == ("app", "logs")
    ] == [("app", "logs", app)]
    shown = capsys.readouterr().err
    assert f"Recent Modal logs:\n{app}: RuntimeError: CUDA error: no kernel image" in shown
    assert f"no running container found for Modal app {app}" in shown
