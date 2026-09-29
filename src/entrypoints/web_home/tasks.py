"""Task list, detail, create, and edit over `.vibesys/tasks`, and committing task files."""

from __future__ import annotations

import contextlib
import hashlib
import shlex
import tomllib
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from entrypoints.web_home.context import atomic_write, parse_body, validation_errors
from entrypoints.web_home.contract import (
    ApiError,
    ErrorCode,
    ResultContract,
    TaskCreate,
    TaskDetail,
    TaskEdit,
    TaskForm,
    TaskList,
    TaskSummary,
)
from entrypoints.web_home.projects import resolve_project
from vibesys.api.request import InputManifest, load_project_task, render_input_manifest
from vs_project.api import Project, ProjectError, TaskExistsError, TaskNotFoundError

if TYPE_CHECKING:
    from entrypoints.web_home.context import Request
    from vs_project.api import TaskDirectory


def project_of(request: Request) -> Project:
    """Open the project named by the first path parameter."""
    root = resolve_project(request.config, request.params[0])
    try:
        return Project.open(root)
    except ProjectError as error:
        raise ApiError(ErrorCode.UNKNOWN_PROJECT, str(error)) from None


def select_task(project: Project, name: str) -> TaskDirectory:
    """Return one task by name with typed errors."""
    try:
        return project.select_task(name)
    except TaskNotFoundError:
        message = f"no task named {name!r}"
        raise ApiError(ErrorCode.UNKNOWN_TASK, message) from None
    except (ProjectError, ValueError) as error:
        raise ApiError(ErrorCode.TASK_INVALID, str(error)) from None


def content_hash(task: TaskDirectory) -> str:
    """Hash both task files; an edit must name the hash it was based on."""
    digest = hashlib.sha256(task.objective_path.read_bytes())
    digest.update(b"\0")
    digest.update(task.manifest_path.read_bytes())
    return digest.hexdigest()


def read_only_reason(manifest: InputManifest) -> str | None:
    """Return why the form cannot edit *manifest* without losing information, or ``None``."""
    if manifest.accuracy.command is None or manifest.benchmark.command is None:
        return "evaluator entrypoint commands are edited in vibesys.input.toml"
    if manifest.benchmark.result is None:
        return "only tasks with a [benchmark.result] metric are editable here"
    rendered = InputManifest.model_validate(tomllib.loads(render_input_manifest(manifest)))
    if rendered != manifest:
        return "the manifest has settings this editor would drop; edit vibesys.input.toml"
    return None


def _result(manifest: InputManifest) -> ResultContract:
    benchmark = manifest.benchmark
    if benchmark.result is not None:
        return ResultContract(
            kind="metric",
            json_argument=benchmark.result.json_argument,
            metric=benchmark.result.metric,
        )
    if benchmark.result_protocol is not None:
        return ResultContract(kind="protocol", protocol_version=benchmark.result_protocol)
    return ResultContract(kind="none")


def detail(project: Project, task: TaskDirectory) -> TaskDetail:
    """Load one task into its API shape, or raise ``task_invalid`` with the loader's message."""
    try:
        bundle = load_project_task(project, task)
    except (OSError, ValueError, ProjectError) as error:
        raise ApiError(ErrorCode.TASK_INVALID, str(error)) from None
    manifest = bundle.manifest
    reason = read_only_reason(manifest)
    return TaskDetail(
        name=task.name.value,
        objective=bundle.objective,
        domain=manifest.agent.domain,
        accuracy_command=bundle.accuracy_command_display,
        benchmark_command=bundle.benchmark_command_display,
        result=_result(manifest),
        profile_guided=manifest.profile_guided is not None,
        editable=reason is None,
        read_only_reason=reason,
        content_hash=content_hash(task),
    )


def task_list(request: Request) -> TaskList:
    """``GET /api/projects/{id}/tasks``."""
    project = project_of(request)
    try:
        tasks = project.discover_tasks() if project.is_initialized() else ()
    except ProjectError as error:
        raise ApiError(ErrorCode.TASK_INVALID, str(error)) from None
    summaries: list[TaskSummary] = []
    for task in tasks:
        try:
            summaries.append(
                TaskSummary(name=task.name.value, valid=True, domain=detail(project, task).domain)
            )
        except ApiError as error:
            summaries.append(TaskSummary(name=task.name.value, valid=False, error=error.message))
    return TaskList(tasks=summaries)


def task_detail(request: Request) -> TaskDetail:
    """``GET /api/projects/{id}/tasks/{name}``."""
    project = project_of(request)
    return detail(project, select_task(project, request.params[1]))


def _argv(raw: str, field: str) -> list[str]:
    try:
        parts = shlex.split(raw)
    except ValueError as error:
        message = f"{field}: {error}"
        raise ApiError(ErrorCode.TASK_INVALID, message) from None
    if not parts:
        message = f"{field} must contain a command"
        raise ApiError(ErrorCode.TASK_INVALID, message)
    return parts


def build_manifest(form: TaskForm, base: InputManifest | None) -> InputManifest:
    """Apply the form to *base* (or a new scalar-metric task) and validate the result."""
    data: dict[str, Any] = (
        base.model_dump(mode="json", exclude_none=True) if base is not None else {"version": 1}
    )
    data["agent"] = {"domain": form.domain.value}
    data["accuracy"] = {
        **data.get("accuracy", {}),
        "command": _argv(form.accuracy_command, "accuracy_command"),
    }
    data["benchmark"] = {
        **data.get("benchmark", {}),
        "command": _argv(form.benchmark_command, "benchmark_command"),
        "result": {"json_argument": form.result_json_argument, "metric": form.result_metric},
    }
    try:
        return InputManifest.model_validate(data)
    except ValidationError as error:
        message = "the task manifest is invalid"
        raise ApiError(
            ErrorCode.TASK_INVALID, message, details={"errors": validation_errors(error)}
        ) from None


def create_task(request: Request) -> TaskDetail:
    """``POST /api/projects/{id}/tasks``: write OBJECTIVE.md and vibesys.input.toml."""
    body = parse_body(request, TaskCreate)
    project = project_of(request)
    manifest = render_input_manifest(build_manifest(body, None))
    with request.config.write_lock:
        try:
            task = project.create_task(body.name, objective=body.objective, manifest=manifest)
        except TaskExistsError as error:
            raise ApiError(ErrorCode.TASK_EXISTS, str(error)) from None
        except (ProjectError, ValueError) as error:
            raise ApiError(ErrorCode.TASK_INVALID, str(error)) from None
    return detail(project, task)


def edit_task(request: Request) -> TaskDetail:
    """``PUT /api/projects/{id}/tasks/{name}``: replace both files if nothing changed since."""
    body = parse_body(request, TaskEdit)
    project = project_of(request)
    with request.config.write_lock:
        task = select_task(project, request.params[1])
        current = detail(project, task)
        if current.content_hash != body.base_hash:
            message = "the task changed on disk since it was loaded; reload it"
            raise ApiError(ErrorCode.TASK_CONFLICT, message)
        if current.read_only_reason is not None:
            raise ApiError(ErrorCode.TASK_READ_ONLY, current.read_only_reason)
        manifest = build_manifest(body, load_project_task(project, task).manifest)
        rendered = render_input_manifest(manifest)
        try:
            tomllib.loads(rendered)
        except tomllib.TOMLDecodeError as error:
            message = f"the rendered task manifest is invalid TOML: {error}"
            raise ApiError(ErrorCode.TASK_INVALID, message) from None
        previous_objective = task.objective_path.read_bytes()
        try:
            atomic_write(task.objective_path, body.objective.encode("utf-8"), mode=0o644)
        except OSError as error:
            message = f"failed to write the task objective: {error}"
            raise ApiError(ErrorCode.INTERNAL, message) from None
        try:
            atomic_write(task.manifest_path, rendered.encode("utf-8"), mode=0o644)
        except OSError as error:
            with contextlib.suppress(OSError):
                atomic_write(task.objective_path, previous_objective, mode=0o644)
            message = f"failed to write the task manifest: {error}"
            raise ApiError(ErrorCode.INTERNAL, message) from None
    return detail(project, task)
