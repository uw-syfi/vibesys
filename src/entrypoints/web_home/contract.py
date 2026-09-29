"""JSON contract of the home server API: request bodies, responses, and errors.

This module is the one authoritative definition of the contract. Clients
generate their types from the JSON Schema printed by
``python -m entrypoints.web_home.contract``.
"""

from __future__ import annotations

import json
import sys
from enum import StrEnum
from http import HTTPStatus
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr
from pydantic.alias_generators import to_camel

from vibesys.api import ComputeBackend, DomainName, RunStatus
from vs_agent.api import Driver


class ErrorCode(StrEnum):
    """Every typed error the API returns, as ``error.code``."""

    UNAUTHORIZED = "unauthorized"
    FORBIDDEN_ORIGIN = "forbidden_origin"
    NOT_FOUND = "not_found"
    INVALID_REQUEST = "invalid_request"
    INTERNAL = "internal_error"
    INVALID_PATH = "invalid_path"
    OUTSIDE_ROOTS = "outside_roots"
    PERMISSION_DENIED = "permission_denied"
    UNKNOWN_PROJECT = "unknown_project"
    NOT_GIT = "not_git"
    NO_COMMITS = "no_commits"
    DIRTY_TREE = "dirty_tree"
    UNINITIALIZED = "uninitialized"
    NO_TASKS = "no_tasks"
    UNKNOWN_PROVIDER = "unknown_provider"
    INVALID_KEY = "invalid_key"
    SYMLINK_REJECTED = "symlink_rejected"
    UNKNOWN_TASK = "unknown_task"
    TASK_INVALID = "task_invalid"
    TASK_EXISTS = "task_exists"
    TASK_CONFLICT = "task_conflict"
    TASK_READ_ONLY = "task_read_only"
    COMMIT_FAILED = "commit_failed"
    UNKNOWN_RUN = "unknown_run"
    ALREADY_LIVE = "already_live"
    LAUNCH_FAILED = "launch_failed"
    PROFILE_GUIDED_UNAVAILABLE = "profile_guided_unavailable"
    BUDGET_DECREASE = "budget_decrease"
    NOT_RESUMABLE = "not_resumable"


_STATUS: dict[ErrorCode, HTTPStatus] = {
    ErrorCode.UNAUTHORIZED: HTTPStatus.UNAUTHORIZED,
    ErrorCode.FORBIDDEN_ORIGIN: HTTPStatus.FORBIDDEN,
    ErrorCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
    ErrorCode.INVALID_REQUEST: HTTPStatus.BAD_REQUEST,
    ErrorCode.INTERNAL: HTTPStatus.INTERNAL_SERVER_ERROR,
    ErrorCode.INVALID_PATH: HTTPStatus.BAD_REQUEST,
    ErrorCode.OUTSIDE_ROOTS: HTTPStatus.FORBIDDEN,
    ErrorCode.PERMISSION_DENIED: HTTPStatus.FORBIDDEN,
    ErrorCode.UNKNOWN_PROJECT: HTTPStatus.NOT_FOUND,
    ErrorCode.NOT_GIT: HTTPStatus.CONFLICT,
    ErrorCode.NO_COMMITS: HTTPStatus.CONFLICT,
    ErrorCode.DIRTY_TREE: HTTPStatus.CONFLICT,
    ErrorCode.UNINITIALIZED: HTTPStatus.CONFLICT,
    ErrorCode.NO_TASKS: HTTPStatus.CONFLICT,
    ErrorCode.UNKNOWN_PROVIDER: HTTPStatus.NOT_FOUND,
    ErrorCode.INVALID_KEY: HTTPStatus.BAD_REQUEST,
    ErrorCode.SYMLINK_REJECTED: HTTPStatus.CONFLICT,
    ErrorCode.UNKNOWN_TASK: HTTPStatus.NOT_FOUND,
    ErrorCode.TASK_INVALID: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.TASK_EXISTS: HTTPStatus.CONFLICT,
    ErrorCode.TASK_CONFLICT: HTTPStatus.CONFLICT,
    ErrorCode.TASK_READ_ONLY: HTTPStatus.CONFLICT,
    ErrorCode.COMMIT_FAILED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.UNKNOWN_RUN: HTTPStatus.NOT_FOUND,
    ErrorCode.ALREADY_LIVE: HTTPStatus.CONFLICT,
    ErrorCode.LAUNCH_FAILED: HTTPStatus.BAD_GATEWAY,
    ErrorCode.PROFILE_GUIDED_UNAVAILABLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.BUDGET_DECREASE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.NOT_RESUMABLE: HTTPStatus.UNPROCESSABLE_ENTITY,
}


class ApiError(Exception):
    """A typed API failure; the server renders it as an ``ErrorBody``."""

    def __init__(
        self, code: ErrorCode, message: str, *, details: dict[str, JsonValue] | None = None
    ) -> None:
        """Carry the code, a user-facing message, and optional structured details."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    @property
    def status(self) -> HTTPStatus:
        """Return the HTTP status this error is sent with."""
        return _STATUS[self.code]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ErrorDetail(_Model):
    """The ``error`` member of every non-2xx response."""

    code: ErrorCode
    message: str
    details: dict[str, JsonValue] | None = None


class ErrorBody(_Model):
    """Every non-2xx response body."""

    error: ErrorDetail

    @classmethod
    def of(cls, error: ApiError) -> ErrorBody:
        """Render one ``ApiError``."""
        return cls(error=ErrorDetail(code=error.code, message=error.message, details=error.details))


# Group (a): folders, projects, catalog, auth.


class FsEntry(_Model):
    """One folder the picker can open; ``path`` is canonical (symlinks resolved)."""

    name: str
    path: str
    git: bool


class FsListing(_Model):
    """A folder's subfolders; ``path`` is null when listing the granted roots."""

    path: str | None
    parent: str | None
    entries: list[FsEntry]


class ValidateRequest(_Model):
    """Body of ``POST /api/projects/validate``."""

    path: str


class ProjectState(StrEnum):
    """Readiness of a folder as a VibeSys project, first blocker wins."""

    MISSING = "missing"
    NOT_GIT = "not_git"
    INVALID = "invalid"
    UNINITIALIZED = "uninitialized"
    NO_TASKS = "no_tasks"
    NO_COMMITS = "no_commits"
    DIRTY_TREE = "dirty_tree"
    READY = "ready"


class ProjectRef(_Model):
    """A project the API can address by ``id`` in later URLs."""

    id: str
    root: str
    name: str


class ProjectValidation(_Model):
    """Response of ``POST /api/projects/validate``."""

    state: ProjectState
    path: str
    project: ProjectRef | None = None
    message: str | None = None
    tasks: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)


class RecentProject(ProjectRef):
    """One recent-projects entry, most recent first."""

    last_opened: str


class ProjectList(_Model):
    """Response of ``GET /api/projects`` and the on-disk recent-projects file."""

    projects: list[RecentProject]


class DriverOption(_Model):
    """One agent driver and the providers it runs."""

    driver: Driver
    providers: list[str]
    supports_docker: bool


class ProviderOption(_Model):
    """One shipped CLI provider and its model suggestions."""

    provider: str
    display_name: str
    supports_reasoning_effort: bool
    suggested_models: list[str]


class LoopBudget(_Model):
    """The total-budget flag an outer loop takes and its CLI default."""

    flag: Literal["--max-rounds", "--max-generations"]
    default: int


class OuterLoopOption(_Model):
    """One outer loop the start form offers."""

    id: str
    budget: LoopBudget
    requires_profile_guided: bool
    roles: list[str]


class Catalog(_Model):
    """Response of ``GET /api/agents/catalog``."""

    drivers: list[DriverOption]
    providers: list[ProviderOption]
    outer_loops: list[OuterLoopOption]
    compute_backends: list[ComputeBackend]
    default_compute_backend: ComputeBackend


class KeyVar(_Model):
    """Where one allowlisted key variable is set; the value is never returned."""

    name: str
    source: Literal["env", "dotenv", "missing"]
    shadowed: bool


class ProviderAuth(_Model):
    """Sign-in state of one provider."""

    provider: str
    display_name: str
    status: Literal["key", "cli_session", "missing"]
    keys: list[KeyVar]
    cli_session: Literal["present", "absent", "unknown"]
    login_command: str


class AuthStatus(_Model):
    """Response of ``GET /api/auth``."""

    providers: list[ProviderAuth]
    dotenv_path: str


class KeyWrite(_Model):
    """Body of ``PUT /api/auth/{provider}``; ``value`` never leaves the server."""

    name: str
    value: SecretStr


class KeyWriteResult(_Model):
    """Response of ``PUT /api/auth/{provider}``."""

    provider: str
    name: str
    status: Literal["unverified"]
    shadowed_by_env: bool


# Group (b): tasks and commit.


class TaskSummary(_Model):
    """One task in ``GET .../tasks``; ``error`` is set when it does not load."""

    name: str
    valid: bool
    domain: str | None = None
    error: str | None = None


class TaskList(_Model):
    """Response of ``GET /api/projects/{id}/tasks``."""

    tasks: list[TaskSummary]


class ResultContract(_Model):
    """How the benchmark reports its score."""

    kind: Literal["metric", "protocol", "none"]
    json_argument: str | None = None
    metric: str | None = None
    protocol_version: int | None = None


class TaskDetail(_Model):
    """Response of task detail, create, and edit."""

    name: str
    objective: str
    domain: DomainName
    accuracy_command: str
    benchmark_command: str
    result: ResultContract
    profile_guided: bool
    editable: bool
    read_only_reason: str | None
    content_hash: str


class TaskForm(_Model):
    """The editable task fields; commands are shell-quoted strings split with ``shlex``."""

    objective: str = Field(min_length=1)
    domain: DomainName
    accuracy_command: str
    benchmark_command: str
    result_json_argument: str
    result_metric: str


class TaskCreate(TaskForm):
    """Body of ``POST .../tasks``."""

    name: str


class TaskEdit(TaskForm):
    """Body of ``PUT .../tasks/{name}``; ``base_hash`` is the detail's ``content_hash``."""

    base_hash: str


class CommitPreview(_Model):
    """Pending changes split into committable task files and everything else."""

    task_files: list[str]
    other: list[str]


class CommitRequest(_Model):
    """Body of ``POST .../commit``: exactly the previewed ``task_files``."""

    paths: list[str]
    message: str | None = None


class CommitResult(_Model):
    """Response of ``POST .../commit``."""

    commit: str
    committed: list[str]


# Group (c): runs and notes.


class GatewayState(StrEnum):
    """What the app can do with a run's gateway."""

    LIVE = "live"
    STARTING = "starting"
    ENDED_SERVING = "ended_serving"
    FAILED = "failed"
    STALE = "stale"
    EXTERNAL = "external"
    REOPENED = "reopened"
    NONE = "none"


class Gateway(_Model):
    """A run gateway; connection fields are set only when it answers health probes."""

    state: GatewayState
    url: str | None = None
    websocket_url: str | None = None
    token: str | None = None
    stderr_tail: list[str] = Field(default_factory=list)
    stderr_log: str | None = None
    origin_mismatch: bool = False


class RunRow(_Model):
    """One run, newest first; ``reopen`` is its read-only gateway when one is serving."""

    run_id: str
    loop: str | None
    status: RunStatus
    rounds: int
    gateway: Gateway
    reopen: Gateway | None = None
    error: str | None = None
    task: str | None = None
    objective: str | None = None
    created_at: str | None = None


class RunList(_Model):
    """Response of ``GET /api/projects/{id}/runs``."""

    runs: list[RunRow]


class RoleOverride(_Model):
    """Per-role model controls written to ``[agent.roles.<id>]``."""

    model: str | None = Field(default=None, min_length=1, max_length=256)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=256)


class StartRun(_Model):
    """Body of ``POST /api/projects/{id}/runs``."""

    task: str
    outer_loop: str
    budget: int | None = Field(default=None, ge=1)
    compute_backend: ComputeBackend
    driver: Driver | None = None
    provider: str
    model: str = Field(min_length=1, max_length=256)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=256)
    roles: dict[str, RoleOverride] = Field(default_factory=dict)


class ResumeRun(_Model):
    """Body of ``POST .../runs/{run}/resume``; ``budget`` may only grow."""

    budget: int | None = Field(default=None, ge=1)


class LaunchResult(_Model):
    """Response of start, resume, and open."""

    run_id: str
    gateway: Gateway


class StopResult(_Model):
    """Response of ``DELETE /api/projects/{id}/live``."""

    stopped: bool
    run_id: str | None


class NoteRecord(BaseModel):
    """A TUI-compatible note file (camelCase on the wire and on disk)."""

    model_config = ConfigDict(
        extra="ignore", frozen=True, alias_generator=to_camel, populate_by_name=True
    )

    run_id: str
    text: str
    created_at: str
    updated_at: str


class NoteResponse(_Model):
    """Response of ``GET``/``PUT /api/notes/{run}``."""

    note: NoteRecord | None


class NoteUpdate(_Model):
    """Body of ``PUT /api/notes/{run}``."""

    text: str


SCHEMA_MODELS: tuple[type[BaseModel], ...] = (
    ErrorBody,
    FsListing,
    ValidateRequest,
    ProjectValidation,
    ProjectList,
    Catalog,
    AuthStatus,
    KeyWrite,
    KeyWriteResult,
    TaskList,
    TaskDetail,
    TaskCreate,
    TaskEdit,
    CommitPreview,
    CommitRequest,
    CommitResult,
    RunList,
    StartRun,
    ResumeRun,
    LaunchResult,
    StopResult,
    NoteResponse,
    NoteUpdate,
)


def main() -> None:
    """Print the JSON Schema of every request and response model."""
    schema = {model.__name__: model.model_json_schema(by_alias=True) for model in SCHEMA_MODELS}
    sys.stdout.write(json.dumps(schema, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
