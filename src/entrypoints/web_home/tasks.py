"""Task list, detail, create, and edit over `.vibesys/tasks`, and committing task files."""

from __future__ import annotations

import hashlib
import tomllib
from typing import TYPE_CHECKING

from entrypoints.web_home.contract import (
    ApiError,
    ErrorCode,
    ResultContract,
    TaskDetail,
    TaskList,
    TaskSummary,
)
from entrypoints.web_home.projects import resolve_project
from vibesys.api.request import InputManifest, load_project_task, render_input_manifest
from vs_project.api import Project, ProjectError, TaskNotFoundError

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
