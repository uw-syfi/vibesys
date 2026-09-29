"""Run history with gateway states, and launching, reopening, resuming, and stopping runs.

Discovery records for run servers this home server launches live under
``$VIBESYS_STATE_HOME/web/gateways/<project id>/`` so they never dirty the
candidate repository. A run started elsewhere (the TUI with ``--web``,
``vibesys web live``) publishes the default ``.vibesys/web-gateway.json`` and
is reported as ``external``.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from entrypoints.web_home.catalog import LOOPS
from entrypoints.web_home.context import atomic_write, parse_body, safe_segment, validation_errors
from entrypoints.web_home.contract import (
    ApiError,
    ErrorCode,
    Gateway,
    GatewayState,
    LaunchResult,
    ProjectState,
    StartRun,
)
from entrypoints.web_home.projects import inspect_project, project_id, resolve_project
from entrypoints.web_home.tasks import select_task
from server.runtime import WebInstanceRecord
from vibesys.api import Config
from vibesys.api.request import generate_experiment_name, load_project_task
from vs_agent.api import SHIPPED_PROVIDERS, agent_catalog
from vs_project.api import Project, ProjectError

if TYPE_CHECKING:
    from pathlib import Path

    from entrypoints.web_home.context import HomeConfig, Request

_RECORD_POLL_SECONDS = 0.05
_REAP_SECONDS = 5.0
_STDERR_TAIL_BYTES = 16_384
_STDERR_TAIL_LINES = 40
_BLOCKERS = {
    ProjectState.MISSING: ErrorCode.INVALID_PATH,
    ProjectState.NOT_GIT: ErrorCode.NOT_GIT,
    ProjectState.INVALID: ErrorCode.TASK_INVALID,
    ProjectState.UNINITIALIZED: ErrorCode.UNINITIALIZED,
    ProjectState.NO_TASKS: ErrorCode.NO_TASKS,
    ProjectState.NO_COMMITS: ErrorCode.NO_COMMITS,
    ProjectState.DIRTY_TREE: ErrorCode.DIRTY_TREE,
}


@dataclass(frozen=True)
class Launch:
    """A run server this process spawned; a daemon thread reaps it when it exits."""

    run_id: str
    process: subprocess.Popen[bytes]
    stderr_log: Path


class Owner(BaseModel):
    """Sidecar ``<record>.owner.json``: which launch published a home-owned record.

    Written atomically after each spawn. Plan 1's live records carry no run id,
    and the pid ties the record to exactly one launch attempt, so a record from
    an earlier attempt is never attributed to a retry.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    pid: int
    origin: str


def _gateway_dir(config: HomeConfig, root: Path) -> Path:
    return config.state_home / "web" / "gateways" / project_id(root)


def _live_record(config: HomeConfig, root: Path) -> Path:
    return _gateway_dir(config, root) / "live.json"


def _run_config(config: HomeConfig, root: Path, run_id: str) -> Path:
    return _gateway_dir(config, root) / f"{safe_segment(run_id)}.agent.toml"


def _external_record(project: Project) -> Path:
    return project.configuration_path() / "web-gateway.json"


def _owner_path(record_path: Path) -> Path:
    return record_path.with_suffix(".owner.json")


def _read_owner(record_path: Path) -> Owner | None:
    try:
        return Owner.model_validate_json(_owner_path(record_path).read_bytes())
    except (OSError, ValidationError):
        return None


def stderr_tail(log: Path) -> list[str]:
    """Return the last lines a run server wrote to stderr."""
    try:
        size = log.stat().st_size
        with log.open("rb") as stream:
            stream.seek(max(0, size - _STDERR_TAIL_BYTES))
            text = stream.read().decode("utf-8", "replace")
    except OSError:
        return []
    return text.splitlines()[-_STDERR_TAIL_LINES:]


def _connected(
    record: WebInstanceRecord, state: GatewayState, *, mismatch: bool = False
) -> Gateway:
    return Gateway(
        state=state,
        url=record.url,
        websocket_url=f"ws://127.0.0.1:{record.port}/ws?token={record.token}",
        token=record.token,
        origin_mismatch=mismatch,
    )


def _failure(log: Path, message: str) -> ApiError:
    details: dict[str, JsonValue] = {"stderr_tail": [*stderr_tail(log)], "stderr_log": str(log)}
    return ApiError(ErrorCode.LAUNCH_FAILED, message, details=details)


def _reap(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=_REAP_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _spawn(
    config: HomeConfig, root: Path, record_path: Path, run_id: str, arguments: list[str]
) -> WebInstanceRecord:
    """Start a detached run server; return once it publishes *record_path* (gateway ready).

    A child that exits first, or publishes nothing within ``config.launch_timeout``,
    is reported as ``launch_failed``; a timed-out child is terminated and reaped
    before the error returns, so a retry never races it.
    """
    record_path.parent.mkdir(parents=True, exist_ok=True)
    _owner_path(record_path).unlink(missing_ok=True)
    origins = (config.origin, *config.dev_origins)
    argv = [
        *config.run_server_argv,
        *("--web", "--detach", "--web-port", "0", "--web-instance", str(record_path)),
        *(item for origin in origins for item in ("--web-origin", origin)),
        *arguments,
    ]
    # VIBESYS_DETACHED_CHILD makes entrypoints.server serve in this child instead of
    # re-spawning with stderr discarded; BROWSER=true stops it opening a browser tab.
    environment = {**config.environ, "VIBESYS_DETACHED_CHILD": "1", "BROWSER": "true"}
    log = record_path.with_suffix(".stderr.log")
    with log.open("wb") as stderr:
        process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-101308 [S603]; argv is the fixed run-server command plus validated flags, never a shell string.
            # > `entrypoints.server --detach` re-spawns with stderr discarded, and the
            # > launch_failed contract returns that stderr; shell=True weakens argv safety.
            argv,
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            start_new_session=True,
        )
    threading.Thread(target=process.wait, name=f"reap-{process.pid}", daemon=True).start()
    owner = Owner(run_id=run_id, pid=process.pid, origin=config.origin)
    atomic_write(_owner_path(record_path), owner.model_dump_json().encode(), mode=0o600)
    config.launches[record_path] = Launch(run_id=run_id, process=process, stderr_log=log)
    deadline = time.monotonic() + config.launch_timeout
    while time.monotonic() < deadline:
        record = WebInstanceRecord.discover(record_path, cleanup_stale=False)
        if record is not None and record.pid == process.pid:
            return record
        if process.poll() is not None:
            raise _failure(log, f"the run server exited with status {process.returncode}")
        time.sleep(_RECORD_POLL_SECONDS)
    _reap(process)
    raise _failure(log, f"the run server published no gateway within {config.launch_timeout:g} s")


def _owner_run(
    path: Path, record: WebInstanceRecord | None, project: Project, *, external: bool
) -> str | None:
    """Return the run a live record belongs to; live records carry no run id themselves."""
    if external:
        with contextlib.suppress(ProjectError):
            return project.state.current_run_id()
        return None
    owner = _read_owner(path)
    if owner is None or (record is not None and record.pid != owner.pid):
        return None
    return owner.run_id


def _require_no_live(config: HomeConfig, root: Path) -> None:
    project = Project.open(root)
    for path, external in ((_live_record(config, root), False), (_external_record(project), True)):
        record = WebInstanceRecord.discover(path)
        if record is not None:
            message = "this project already has a live run; stop it first"
            run_id = _owner_run(path, record, project, external=external)
            raise ApiError(ErrorCode.ALREADY_LIVE, message, details={"run_id": run_id})


def _require_ready(root: Path) -> None:
    validation = inspect_project(root)
    if validation.state is not ProjectState.READY:
        message = f"the project is not ready to launch: {validation.state.value}"
        raise ApiError(
            _BLOCKERS[validation.state], message, details={"pending": [*validation.pending]}
        )


def render_run_config(body: StartRun) -> str:
    """Render the run-owned agent TOML passed with ``--config``, validated as ``Config``."""
    agent: dict[str, object] = {"backend": "cli", "cli_provider": body.provider}
    if body.driver is not None:
        agent["driver"] = body.driver.value
    roles = {
        role: fields
        for role, override in sorted(body.roles.items())
        if (fields := override.model_dump(exclude_none=True))
    }
    raw: dict[str, object] = {"model": {"name": body.model}, "agent": {**agent, "roles": roles}}
    if body.reasoning_effort is not None:
        raw["thinking"] = {"level": body.reasoning_effort}
    try:
        Config.model_validate(raw)
    except ValidationError as error:
        message = "the run configuration is invalid"
        raise ApiError(
            ErrorCode.INVALID_REQUEST, message, details={"errors": validation_errors(error)}
        ) from None
    lines = ["[model]", f"name = {json.dumps(body.model)}"]
    if body.reasoning_effort is not None:
        lines += ["", "[thinking]", f"level = {json.dumps(body.reasoning_effort)}"]
    lines += ["", "[agent]", *(f"{key} = {json.dumps(value)}" for key, value in agent.items())]
    for role, fields in roles.items():
        lines += ["", f"[agent.roles.{json.dumps(role)}]"]
        lines += [f"{key} = {json.dumps(value)}" for key, value in fields.items()]
    return "\n".join(lines) + "\n"


def _check_start(body: StartRun) -> None:
    if body.outer_loop not in LOOPS:
        message = f"unknown outer loop {body.outer_loop!r}; choose from {', '.join(LOOPS)}"
        raise ApiError(ErrorCode.INVALID_REQUEST, message)
    providers = (
        agent_catalog()[body.driver].providers if body.driver is not None else SHIPPED_PROVIDERS
    )
    if body.provider not in providers:
        message = f"provider {body.provider!r} is not available for this driver"
        raise ApiError(ErrorCode.UNKNOWN_PROVIDER, message)


def _start_arguments(body: StartRun, root: Path, run_id: str, run_config: Path) -> list[str]:
    arguments = [
        *("--project", str(root), "--task", body.task, "--outer-loop", body.outer_loop),
        *("--exp-name", run_id, "--config", str(run_config)),
        *("--backend", body.compute_backend.value, "--cli-provider", body.provider),
    ]
    if body.budget is not None:
        arguments += [LOOPS[body.outer_loop][0], str(body.budget)]
    return arguments


def start_run(request: Request) -> LaunchResult:
    """``POST /api/projects/{id}/runs``: launch a run; return once its gateway is ready."""
    body = parse_body(request, StartRun)
    _check_start(body)
    config = request.config
    root = resolve_project(config, request.params[0])
    with config.launch_lock:
        _require_no_live(config, root)
        _require_ready(root)
        project = Project.open(root)
        manifest = load_project_task(project, select_task(project, body.task)).manifest
        if body.outer_loop == "profile-guided" and manifest.profile_guided is None:
            message = (
                "the profile-guided loop needs a [profile_guided] section in the task manifest"
            )
            raise ApiError(ErrorCode.PROFILE_GUIDED_UNAVAILABLE, message)
        run_id = generate_experiment_name(root)
        run_config = _run_config(config, root, run_id)
        atomic_write(run_config, render_run_config(body).encode("utf-8"), mode=0o600)
        arguments = _start_arguments(body, root, run_id, run_config)
        record = _spawn(config, root, _live_record(config, root), run_id, arguments)
    return LaunchResult(run_id=run_id, gateway=_connected(record, GatewayState.STARTING))
