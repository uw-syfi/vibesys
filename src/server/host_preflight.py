"""Light contracts for starting a run from the desktop app: typed options and preflight checks.

``RunOptions`` is what the New run screen sets on ``run.start`` (feature ``run_options``);
``Check`` (with an optional typed ``Cause``) and ``Fix`` are what ``run.preflight`` (feature ``preflight``) answers, closed sets
so every failure the app shows carries an action it knows how to perform. ``PreflightReport``
is what ``vibesys preflight`` prints: the checks only the run's own code can make, from the
same arguments ``run.start`` would launch with. Pydantic and the standard library only, like
``server.host_api``, which embeds these in the helper's messages.

Public surface: ``CheckId``, ``CheckState``, ``ChooseCheckout``, ``CopyOut``, ``DeclareBuild``,
``EditSetting``, ``StartDocker``, ``Fix``, ``Check``, ``ReasoningEffort``, ``RoleName``,
``RoleOption``, ``RunResults``,
``RunOptions``, ``SettingsValid``, ``SettingsInvalid``, ``PreflightReport``, ``one_line``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, StringConstraints, model_validator

DETAIL_MAX_CHARACTERS = 300
"""The longest ``Check.detail``."""

Detail = Annotated[
    str,
    StringConstraints(min_length=1, max_length=DETAIL_MAX_CHARACTERS, pattern=r"^[^\n\r]*$"),
]


def one_line(text: str, fallback: str) -> str:
    """The first non-blank line of ``text`` cut to ``DETAIL_MAX_CHARACTERS``, else ``fallback``."""
    line = next((each.strip() for each in text.splitlines() if each.strip()), fallback)
    if len(line) <= DETAIL_MAX_CHARACTERS:
        return line
    return line[: DETAIL_MAX_CHARACTERS - 1] + "…"


class CheckId(StrEnum):
    """What a preflight check is about; closed."""

    CHECKOUT_FOUND = "checkout_found"
    PROJECT_OWN_REPOSITORY = "project_own_repository"
    PROJECT_CLEAN = "project_clean"
    SETTINGS_VALID = "settings_valid"
    DOCKER_REACHABLE = "docker_reachable"
    AGENT_IMAGE = "agent_image"
    EVALUATOR_AVAILABLE = "evaluator_available"


class Backend(StrEnum):
    """A compute backend a task targets (``vs_sandbox.ComputeBackend``'s values); closed."""

    CPU = "cpu"
    CUDA = "cuda"
    ROCM = "rocm"
    METAL = "metal"
    TRAINIUM = "trainium"


class BackendUnavailable(BaseModel):
    """The machine cannot run the compute backend the task targets.

    A fact, not a sentence: the client words it for its own controls, the CLI for its flags.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["backend_unavailable"] = "backend_unavailable"
    task: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    backend: Backend
    """The backend the run resolved to."""
    declared: tuple[Backend, ...]
    """The backends the task's manifest declares; empty for a task that declares none."""


class ProjectDirty(BaseModel):
    """The project's repository has changes a run cannot start beside.

    A fact, not a sentence: the client words it. ``paths`` are relative to the project, sorted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["project_dirty"] = "project_dirty"
    project: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    """The project directory."""
    paths: tuple[Annotated[str, StringConstraints(min_length=1, max_length=4096)], ...]
    """Every changed path (tracked or untracked), without those the run's own state accounts for."""


Cause = Annotated[BackendUnavailable | ProjectDirty, Field(discriminator="kind")]
"""Why a check failed, as data; ``kind`` discriminates."""


def backend_unavailable_text(cause: BackendUnavailable) -> str:
    """The fact as one plain line, naming no flag or setting."""
    return (
        f"task {cause.task!r} targets the {cause.backend.value!r} backend, "
        "which this machine cannot run"
    )


class CheckState(StrEnum):
    """How a check came out; Start is enabled exactly when none is ``fail``."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


class ChooseCheckout(BaseModel):
    """Pick another VibeSys checkout for this machine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["choose_checkout"] = "choose_checkout"


class CopyOut(BaseModel):
    """Copy the project into its own repository (``project.copy_out``), at the suggested path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["copy_out"] = "copy_out"
    suggested_path: str


class EditSetting(BaseModel):
    """Edit the named setting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["edit_setting"] = "edit_setting"
    key: str


class StartDocker(BaseModel):
    """Start Docker on the machine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["start_docker"] = "start_docker"


class DeclareBuild(BaseModel):
    """Declare a ``[build]`` producing ``outputs`` in the task manifest at ``manifest``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["declare_build"] = "declare_build"
    manifest: str
    outputs: tuple[str, ...]


Fix = Annotated[
    ChooseCheckout | CopyOut | EditSetting | StartDocker | DeclareBuild,
    Field(discriminator="kind"),
]


class Check(BaseModel):
    """One preflight check: its state, one line of detail, and the action that fixes it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: CheckId
    state: CheckState
    detail: Detail
    fix: Fix | None = None
    cause: Annotated[Cause | None, Field(exclude_if=lambda cause: cause is None)] = None
    """The failure as data, when it has a typed cause; ``detail`` says it in one line."""


class ReasoningEffort(StrEnum):
    """A role's reasoning effort; ``default`` keeps the configured one."""

    DEFAULT = "default"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


RoleName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]*$", max_length=64)]
"""An agent role's id, as an orchestration plugin declares it (``AgentRole.id``)."""


class RoleOption(BaseModel):
    """One agent role's model and reasoning effort; ``model`` ``None`` keeps the run's model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: RoleName
    model: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = None
    reasoning_effort: ReasoningEffort = ReasoningEffort.DEFAULT


class RunResults(StrEnum):
    """Where a run's results go: kept on the machine, or also published to GitHub."""

    KEEP = "keep"
    PUBLISH_GITHUB = "publish_github"


class RunOptions(BaseModel):
    """What the New run screen sets on a run; ``None`` (or no role) keeps the configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rounds: PositiveInt | None = None
    time_limit_s: PositiveInt | None = None
    results: RunResults | None = None
    roles: tuple[RoleOption, ...] = ()

    @model_validator(mode="after")
    def _one_option_per_role(self) -> RunOptions:
        names = [option.role for option in self.roles]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            message = f"roles names each role once; repeated: {', '.join(repeated)}"
            raise ValueError(message)
        return self


class SettingsValid(BaseModel):
    """The run's configuration passed the validation the run itself runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: Literal[True] = True


class SettingsInvalid(BaseModel):
    """The run's configuration failed; ``message`` names the offending key or argument."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: Literal[False] = False
    code: str
    stage: str
    message: str
    cause: BackendUnavailable | None = None
    """The failure as data when it has a typed cause; ``message`` is the CLI's wording of it."""


class PreflightReport(BaseModel):
    """``vibesys preflight``: the checks only the run's own code makes (version 1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    settings: SettingsValid | SettingsInvalid
    uses_docker: bool | None
    """Whether the run's environment starts local Docker containers; ``None`` when the
    settings did not resolve one. Docker is not checked for a run that needs none."""
    agent_image: Check
    evaluator: Check
    backend: Backend | None = None
    """The compute backend the run resolved to (the task's), ``None`` when the settings did
    not resolve one."""
    project_changes: tuple[str, ...] = ()
    """The project repository's changes that would stop a new run at launch (what the run's own
    tracker calls its baseline changes), relative to the project; empty for a clean one, a project
    that is not a repository yet, and a resume."""
    roles: tuple[RoleName, ...] = ()
    """The role ids the run's orchestration plugin declares, in declaration order: what
    ``RoleOption.role`` may name. Empty when the settings did not resolve an orchestration."""


__all__ = [
    "DETAIL_MAX_CHARACTERS",
    "Backend",
    "BackendUnavailable",
    "Cause",
    "Check",
    "CheckId",
    "CheckState",
    "ChooseCheckout",
    "CopyOut",
    "DeclareBuild",
    "EditSetting",
    "Fix",
    "PreflightReport",
    "ProjectDirty",
    "ReasoningEffort",
    "RoleName",
    "RoleOption",
    "RunOptions",
    "RunResults",
    "SettingsInvalid",
    "SettingsValid",
    "StartDocker",
    "backend_unavailable_text",
    "one_line",
]
