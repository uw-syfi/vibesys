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
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from entrypoints.cli.resume import _POLICY_CLI_SELECTION
from entrypoints.web_home.catalog import LOOPS, budget_destination
from entrypoints.web_home.context import atomic_write, parse_body, safe_segment, validation_errors
from entrypoints.web_home.contract import (
    ApiError,
    ErrorCode,
    Gateway,
    GatewayState,
    LaunchResult,
    ProjectState,
    ResumeRun,
    RunList,
    RunRow,
    StartRun,
    StopResult,
)
from entrypoints.web_home.projects import inspect_project, project_id, resolve_project
from entrypoints.web_home.tasks import select_task
from server.runtime import WebInstanceRecord
from vibesys.api import Config, RunRecordReadError, RunStatus, open_run_store
from vibesys.api.request import generate_experiment_name, load_project_task, orchestration_roles
from vs_agent.api import SHIPPED_PROVIDERS, agent_catalog
from vs_project.api import Project, ProjectError

if TYPE_CHECKING:
    from pathlib import Path

    from entrypoints.web_home.context import HomeConfig, Request
    from vibesys.api import RunStore
    from vs_project.api import OrchestrationRunManifest

_RECORD_POLL_SECONDS = 0.05
_REAP_SECONDS = 5.0
_STDERR_TAIL_BYTES = 16_384
_STDERR_TAIL_LINES = 40
_TOML_FORBIDDEN_CONTROL = re.compile(r"[\x7f-\x9f]")
_BLOCKERS = {
    ProjectState.MISSING: ErrorCode.INVALID_PATH,
    ProjectState.NOT_GIT: ErrorCode.NOT_GIT,
    ProjectState.INVALID: ErrorCode.TASK_INVALID,
    ProjectState.UNINITIALIZED: ErrorCode.UNINITIALIZED,
    ProjectState.NO_TASKS: ErrorCode.NO_TASKS,
    ProjectState.NO_COMMITS: ErrorCode.NO_COMMITS,
    ProjectState.DIRTY_TREE: ErrorCode.DIRTY_TREE,
}
_HISTORY_TAIL_BYTES = 262_144
_ATTEMPT_MARKERS = (
    b'"server_started"',
    b'"experiments_changed"',
    b'"run_finished"',
    b'"run_failed"',
    b'"run_interrupted"',
)
# The controller records a failed run as `run_failed`, not `run_finished` with a failed status.
_TERMINAL = {
    "run_finished": RunStatus.COMPLETED,
    "run_failed": RunStatus.FAILED,
    "run_interrupted": RunStatus.FAILED,
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


def _signal_group(process: subprocess.Popen[bytes], signum: signal.Signals) -> None:
    # The child is a session leader (start_new_session), so its pid is its process group.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signum)


def _reap(process: subprocess.Popen[bytes]) -> None:
    _signal_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=_REAP_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_group(process, signal.SIGKILL)
        process.wait()


def _spawn(
    config: HomeConfig, root: Path, record_path: Path, run_id: str, arguments: list[str]
) -> WebInstanceRecord:
    """Start a detached run server; return once it publishes *record_path* (gateway ready).

    A child that exits first, or publishes nothing within ``config.launch_timeout``,
    is reported as ``launch_failed``; a timed-out child is terminated and reaped
    before the error returns, so a retry never races it.
    """
    settled = [
        path for path, launch in config.launches.items() if launch.process.poll() is not None
    ]
    for path in settled:
        del config.launches[path]
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
    try:
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
    except OSError as error:
        raise _failure(log, f"the run server could not start: {error.strerror}") from None
    threading.Thread(target=process.wait, name=f"reap-{process.pid}", daemon=True).start()
    owner = Owner(run_id=run_id, pid=process.pid, origin=config.origin)
    try:
        atomic_write(_owner_path(record_path), owner.model_dump_json().encode(), mode=0o600)
    except (OSError, ApiError) as error:
        # An untracked child would hold the gateway and block every retry.
        _signal_group(process, signal.SIGKILL)
        process.wait()
        raise _failure(log, f"the launch owner record could not be written: {error}") from None
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


@dataclass(frozen=True)
class _Live:
    path: Path
    record: WebInstanceRecord
    run_id: str | None
    external: bool


def _live(config: HomeConfig, root: Path) -> _Live | None:
    """Return the project's serving live gateway, home-owned first, then external."""
    project = Project.open(root)
    for path, external in ((_live_record(config, root), False), (_external_record(project), True)):
        record = WebInstanceRecord.discover(path)
        if record is not None:
            run_id = _owner_run(path, record, project, external=external)
            return _Live(path=path, record=record, run_id=run_id, external=external)
    return None


def _require_no_live(config: HomeConfig, root: Path) -> None:
    if (live := _live(config, root)) is not None:
        message = "this project already has a live run; stop it first"
        raise ApiError(ErrorCode.ALREADY_LIVE, message, details={"run_id": live.run_id})


def _require_ready(root: Path) -> None:
    validation = inspect_project(root)
    if validation.state is not ProjectState.READY:
        message = f"the project is not ready to launch: {validation.state.value}"
        raise ApiError(
            _BLOCKERS[validation.state], message, details={"pending": [*validation.pending]}
        )


def _toml_string(value: str) -> str:
    # Mirrors vibesys.inputs._manifest._toml_string (private there): json.dumps escapes
    # C0 controls but leaves DEL and C1 literal, which TOML basic strings forbid.
    encoded = json.dumps(value, ensure_ascii=False)
    return _TOML_FORBIDDEN_CONTROL.sub(lambda match: f"\\u{ord(match.group()):04x}", encoded)


def render_run_config(body: StartRun) -> str:
    """Render the run-owned agent TOML passed with ``--config``, validated as ``Config``."""
    agent: dict[str, str] = {"backend": "cli", "cli_provider": body.provider}
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
    lines = ["[model]", f"name = {_toml_string(body.model)}"]
    if body.reasoning_effort is not None:
        lines += ["", "[thinking]", f"level = {_toml_string(body.reasoning_effort)}"]
    lines += ["", "[agent]", *(f"{key} = {_toml_string(value)}" for key, value in agent.items())]
    for role, fields in roles.items():
        lines += ["", f"[agent.roles.{_toml_string(role)}]"]
        lines += [f"{key} = {_toml_string(value)}" for key, value in fields.items()]
    return "\n".join(lines) + "\n"


def _check_start(body: StartRun) -> None:
    if body.outer_loop not in LOOPS:
        message = f"unknown outer loop {body.outer_loop!r}; choose from {', '.join(LOOPS)}"
        raise ApiError(ErrorCode.INVALID_REQUEST, message)
    roles = orchestration_roles(LOOPS[body.outer_loop][2])
    if unknown := sorted(set(body.roles) - set(roles)):
        message = f"unknown roles {unknown} for {body.outer_loop}; choose from {', '.join(roles)}"
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


def _reopen_record(config: HomeConfig, root: Path, run_id: str) -> Path:
    return _gateway_dir(config, root) / f"reopen-{safe_segment(run_id)}.json"


def _load_run(root: Path, run_id: str) -> OrchestrationRunManifest:
    try:
        return Project.open(root).state.load_run(run_id)
    except (ProjectError, ValueError):
        message = f"no run {run_id!r} in this project"
        raise ApiError(ErrorCode.UNKNOWN_RUN, message) from None


def _stop(record: WebInstanceRecord, path: Path, *, group: bool) -> bool:
    """SIGTERM a gateway; return whether it is gone once the wait ends.

    The whole process group is signalled only for a home-owned gateway whose owner
    sidecar names the same pid (a session leader this home spawned). Anything else,
    including an external gateway (the TUI's), gets a plain SIGTERM, as ``web stop``
    sends: its group may be the operator's shell job. No SIGKILL: SIGTERM lets the
    run journal ``RUN_INTERRUPTED`` and tear down.
    """
    owner = _read_owner(path) if group else None
    with contextlib.suppress(ProcessLookupError):
        if owner is not None and owner.pid == record.pid:
            os.killpg(record.pid, signal.SIGTERM)
        else:
            os.kill(record.pid, signal.SIGTERM)
    deadline = time.monotonic() + _REAP_SECONDS
    while WebInstanceRecord.discover(path) is not None and time.monotonic() < deadline:
        time.sleep(_RECORD_POLL_SECONDS)
    return WebInstanceRecord.discover(path) is None


def resume_run(request: Request) -> LaunchResult:
    """``POST .../runs/{run}/resume``: resume; the CLI restores the recorded configuration.

    Resuming the run that is already live returns its gateway instead of a second backend.
    """
    body = parse_body(request, ResumeRun)
    config = request.config
    root = resolve_project(config, request.params[0])
    run_id = request.params[1]
    with config.launch_lock:
        live = _live(config, root)
        if live is not None and live.run_id == run_id and body.budget is None:
            return LaunchResult(run_id=run_id, gateway=_connected(live.record, GatewayState.LIVE))
        descriptor = _load_run(root, run_id).orchestration
        if descriptor.id not in _POLICY_CLI_SELECTION:
            message = f"the app cannot resume orchestration {descriptor.id!r}"
            raise ApiError(ErrorCode.NOT_RESUMABLE, message)
        loop = _POLICY_CLI_SELECTION[descriptor.id][0]
        flag = LOOPS[loop][0]
        recorded = descriptor.options.get(budget_destination(flag))
        if body.budget is not None and isinstance(recorded, int) and body.budget < recorded:
            message = f"{flag} is the run's total limit and cannot go below {recorded}"
            raise ApiError(ErrorCode.BUDGET_DECREASE, message, details={"recorded": recorded})
        _require_no_live(config, root)
        arguments = ["--project", str(root), "--resume", run_id, "--outer-loop", loop]
        if body.budget is not None:
            arguments += [flag, str(body.budget)]
        record = _spawn(config, root, _live_record(config, root), run_id, arguments)
    return LaunchResult(run_id=run_id, gateway=_connected(record, GatewayState.STARTING))


def _reopen_arguments(root: Path, run_id: str) -> list[str]:
    # Sub-project 1: `--web-reopen-run` with `--project` reads the run's own journal and record.
    return ["--project", str(root), "--web-reopen-run", run_id]


def _serving_reopen(
    config: HomeConfig, root: Path, run_id: str
) -> tuple[WebInstanceRecord, bool] | None:
    """Return a serving read-only gateway for *run_id* and whether it rejects our origin.

    Ours is checked first, then plan 1's default record from another launcher.
    """
    external = _external_record(Project.open(root)).with_name(f"web-gateway-{run_id}.json")
    for path in (_reopen_record(config, root, run_id), external):
        record = WebInstanceRecord.discover(path) if path.exists() else None
        if record is not None and (record.mode, record.run_id) == ("reopen", run_id):
            owner = _read_owner(path) if path != external else None
            return record, owner is not None and owner.origin != config.origin
    return None


def open_run(request: Request) -> LaunchResult:
    """``POST .../runs/{run}/open``: serve a run read-only, reusing a gateway that still serves.

    The live run's own gateway is returned as is. A reopen gateway started for an
    older home origin is restarted, since it would reject the app.
    """
    config = request.config
    root = resolve_project(config, request.params[0])
    run_id = request.params[1]
    with config.launch_lock:
        live = _live(config, root)
        if live is not None and live.run_id == run_id:
            return LaunchResult(run_id=run_id, gateway=_connected(live.record, GatewayState.LIVE))
        _load_run(root, run_id)
        path = _reopen_record(config, root, run_id)
        serving = _serving_reopen(config, root, run_id)
        if serving is not None and serving[1]:
            _stop(serving[0], path, group=True)
            serving = None
        if serving is None:
            record = _spawn(config, root, path, run_id, _reopen_arguments(root, run_id))
        else:
            record = serving[0]
    return LaunchResult(run_id=run_id, gateway=_connected(record, GatewayState.REOPENED))


def stop_live(request: Request) -> StopResult:
    """``DELETE /api/projects/{id}/live``: SIGTERM the live gateway; a no-op once it is gone.

    ``stopped`` is whether the gateway is gone; a slow teardown reports false and a
    later call signals it again.
    """
    config = request.config
    root = resolve_project(config, request.params[0])
    with config.launch_lock:
        live = _live(config, root)
        if live is None:
            return StopResult(stopped=False, run_id=None)
        stopped = _stop(live.record, live.path, group=not live.external)
    return StopResult(stopped=stopped, run_id=live.run_id)


@dataclass(frozen=True)
class _Published:
    """One live discovery record file; ``record`` is ``None`` when its gateway does not answer."""

    path: Path
    record: WebInstanceRecord | None
    run_id: str | None
    external: bool
    origin_mismatch: bool


def origin_mismatches(config: HomeConfig) -> list[str]:
    """Describe serving home-launched gateways that only accept another home origin."""
    found: list[str] = []
    for owner_path in sorted((config.state_home / "web" / "gateways").glob("*/*.owner.json")):
        record_path = owner_path.with_name(owner_path.name.removesuffix(".owner.json") + ".json")
        owner = _read_owner(record_path)
        record = WebInstanceRecord.discover(record_path, cleanup_stale=False)
        if owner is not None and record is not None and owner.origin != config.origin:
            found.append(f"{record.project_root} run {owner.run_id} ({owner.origin})")
    return found


def _published(config: HomeConfig, root: Path, project: Project) -> list[_Published]:
    items: list[_Published] = []
    for path, external in ((_live_record(config, root), False), (_external_record(project), True)):
        if not path.exists():
            continue
        record = WebInstanceRecord.discover(path, cleanup_stale=False)
        if record is not None and record.mode != "live":
            continue
        owner = None if external else _read_owner(path)
        items.append(
            _Published(
                path=path,
                record=record,
                run_id=_owner_run(path, record, project, external=external),
                external=external,
                origin_mismatch=owner is not None and owner.origin != config.origin,
            )
        )
    return items


def _attempt(
    root: Path, run_id: str, *, tail_bytes: int | None = None
) -> tuple[bool, RunStatus | None]:
    """Return (record attached, terminal status) of the journal's latest server attempt.

    Every run server starts its journal segment with ``server_started`` (a resume
    appends a new one), so earlier attempts' ``run_finished`` never count. The run
    is attached once ``experiments_changed`` reports ``project_attached``
    (sub-project 1 emits it after the run record attaches).
    """
    # ponytail: rescans the journal per call (the live run full, history rows the tail);
    # keep a per-journal byte offset if large journals make polling slow.
    attached, terminal = False, None
    try:
        # ponytail: log_directory_for prepares the state home on every run list; harmless
        # (idempotent mkdir), switch to a read-only path lookup if it ever shows up in profiles.
        journal = Project.log_directory_for(root, run_id) / "run-events.jsonl"
        size = journal.stat().st_size
        with journal.open("rb") as stream:
            start = 0 if tail_bytes is None else max(0, size - tail_bytes)
            stream.seek(start)
            lines = stream.read().splitlines()
    except (OSError, ProjectError):
        return False, None
    for line in lines[1:] if start else lines:
        if not any(marker in line for marker in _ATTEMPT_MARKERS):
            continue
        event = _event(line)
        kind = event.get("type")
        if kind == "server_started":
            attached, terminal = False, None
        elif kind == "experiments_changed":
            data = event.get("data")
            attached = attached or (
                isinstance(data, dict) and data.get("reason") == "project_attached"
            )
        elif isinstance(kind, str) and kind in _TERMINAL:
            terminal = _TERMINAL[kind]
    return attached, terminal


def _event(line: bytes) -> dict[str, object]:
    try:
        event = json.loads(line)
    except ValueError:
        return {}
    return event if isinstance(event, dict) else {}


def _published_gateway(item: _Published, root: Path, run_id: str) -> Gateway:
    if item.record is None:
        return Gateway(state=GatewayState.STALE)
    if item.external:
        return _connected(item.record, GatewayState.EXTERNAL)
    attached, terminal = _attempt(root, run_id)
    if terminal is not None:
        state = GatewayState.ENDED_SERVING
    else:
        state = GatewayState.LIVE if attached else GatewayState.STARTING
    return _connected(item.record, state, mismatch=item.origin_mismatch)


def _launch_gateway(config: HomeConfig, root: Path, run_id: str) -> Gateway:
    launch = config.launches.get(_live_record(config, root))
    if launch is None or launch.run_id != run_id:
        return Gateway(state=GatewayState.NONE)
    code = launch.process.poll()
    if code is None:
        return Gateway(state=GatewayState.STARTING)
    # A positive status is the run server failing; a negative one is a signal, e.g. our SIGTERM.
    if code > 0:
        return Gateway(
            state=GatewayState.FAILED,
            stderr_tail=stderr_tail(launch.stderr_log),
            stderr_log=str(launch.stderr_log),
        )
    return Gateway(state=GatewayState.NONE)


def _gateway(config: HomeConfig, root: Path, run_id: str, published: list[_Published]) -> Gateway:
    for item in published:
        if item.run_id == run_id:
            return _published_gateway(item, root, run_id)
    return _launch_gateway(config, root, run_id)


def _reopen_gateway(config: HomeConfig, root: Path, run_id: str) -> Gateway | None:
    serving = _serving_reopen(config, root, run_id)
    if serving is None:
        return None
    return _connected(serving[0], GatewayState.REOPENED, mismatch=serving[1])


def _status(root: Path, run_id: str, gateway: Gateway) -> RunStatus:
    """Derive lifecycle from the journal and the gateway; the store always says unknown."""
    if gateway.state is GatewayState.FAILED:
        return RunStatus.FAILED
    if gateway.state in {GatewayState.LIVE, GatewayState.STARTING, GatewayState.EXTERNAL}:
        return RunStatus.ACTIVE
    terminal = _attempt(root, run_id, tail_bytes=_HISTORY_TAIL_BYTES)[1]
    return terminal or RunStatus.UNKNOWN


def _row(
    config: HomeConfig,
    root: Path,
    store: RunStore,
    manifest: OrchestrationRunManifest,
    published: list[_Published],
) -> RunRow:
    run_id = manifest.run_id
    gateway = _gateway(config, root, run_id, published)
    reopen = _reopen_gateway(config, root, run_id)
    status = _status(root, run_id, gateway)
    identity = {"task": manifest.task_name, "created_at": manifest.created_at.isoformat()}
    try:
        view = store.get_run(run_id)
        objective = store.get_record(run_id).facts().effective_objective
    except (RunRecordReadError, ProjectError, ValueError) as error:
        return RunRow(
            run_id=run_id,
            loop=None,
            status=status,
            rounds=0,
            gateway=gateway,
            reopen=reopen,
            error=str(error),
            **identity,
        )
    return RunRow(
        run_id=run_id,
        loop=view.loop,
        status=status,
        rounds=len(view.rounds),
        gateway=gateway,
        reopen=reopen,
        objective=objective,
        **identity,
    )


def list_runs(request: Request) -> RunList:
    """``GET /api/projects/{id}/runs``: runs newest first, each with its gateway state."""
    config = request.config
    root = resolve_project(config, request.params[0])
    project = Project.open(root)
    published = _published(config, root, project)
    try:
        manifests = project.state.list_runs()
    except ProjectError:
        manifests = []
    store = open_run_store(project)
    rows = [
        _row(config, root, store, manifest, published) for manifest in reversed(manifests)
    ]
    known = {row.run_id for row in rows}
    launch = config.launches.get(_live_record(config, root))
    candidates = [item.run_id for item in published] + ([launch.run_id] if launch else [])
    pending = [run_id for run_id in dict.fromkeys(candidates) if run_id and run_id not in known]
    gateways = [(run_id, _gateway(config, root, run_id, published)) for run_id in pending]
    # Launches not yet in the run store have no manifest or record, so task, objective
    # and created_at stay None.
    starting = [
        RunRow(
            run_id=run_id,
            loop=None,
            status=RunStatus.FAILED if gateway.state is GatewayState.FAILED else RunStatus.ACTIVE,
            rounds=0,
            gateway=gateway,
        )
        for run_id, gateway in gateways
        if gateway.state is not GatewayState.NONE
    ]
    return RunList(runs=[*starting, *rows])
