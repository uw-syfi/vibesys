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

from server.host_compute import MeasurementTarget, ModalTarget, SlurmTarget
from server.host_machine_facts import Accelerator

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
    MEASUREMENT_TARGET = "measurement_target"
    """Where results are measured is set up and fits the task; only with ``measure_on``."""


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

    def plain(self) -> str:
        """The fact as one plain line."""
        return backend_unavailable_text(self)


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

    def plain(self) -> str:
        """The fact as one plain line."""
        return f"{len(self.paths)} uncommitted path(s) in the project"


TEXT_MAX_CHARACTERS = 4096
"""The longest name or message a cause carries."""

_Text = Annotated[str, StringConstraints(min_length=1, max_length=TEXT_MAX_CHARACTERS)]
"""A name or a message a cause carries, as the run printed it."""


def clip(text: str) -> str:
    """``text`` cut to ``TEXT_MAX_CHARACTERS``, and ``"(no message)"`` for a blank one."""
    if not text.strip():
        return "(no message)"
    if len(text) <= TEXT_MAX_CHARACTERS:
        return text
    return text[: TEXT_MAX_CHARACTERS - 1] + "…"


class TaskNotFound(BaseModel):
    """The project has no task of that name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["task_not_found"] = "task_not_found"
    task: _Text
    available: tuple[_Text, ...]

    def plain(self) -> str:
        """The fact as one plain line."""
        return f"task {self.task!r} was not found in the project"


class TaskUnusable(BaseModel):
    """The task's files cannot be loaded; ``problem`` is the loader's own account (raw text)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["task_unusable"] = "task_unusable"
    task: _Text | None
    problem: _Text

    def plain(self) -> str:
        """The fact as one plain line."""
        if self.task is None:
            return "the project's task cannot be loaded"
        return f"task {self.task!r} cannot be loaded"


class TaskNeedsProvisioning(BaseModel):
    """The task needs inputs copied into a project of its own, which this run does not do."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["task_needs_provisioning"] = "task_needs_provisioning"
    task: _Text

    def plain(self) -> str:
        """The fact as one plain line."""
        return f"task {self.task!r} needs its sources copied into a project of its own"


class TaskImageUnsupported(BaseModel):
    """The task's Dockerfile cannot be used the way this run would run; ``problem`` is raw text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["task_image_unsupported"] = "task_image_unsupported"
    task: _Text | None
    problem: _Text

    def plain(self) -> str:
        """The fact as one plain line."""
        if self.task is None:
            return "the project's task ships a Dockerfile this run cannot use here"
        return f"task {self.task!r} ships a Dockerfile this run cannot use here"


class SysboxMissing(BaseModel):
    """The task runs Docker inside its sandbox, which needs a runtime this machine's Docker lacks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["sysbox_missing"] = "sysbox_missing"
    task: _Text
    problem: _Text
    """Why the runtime is missing, as the run's own check says it (raw text)."""

    def plain(self) -> str:
        """The fact as one plain line."""
        return f"task {self.task!r} needs Docker with the Sysbox runtime, which this machine lacks"


class ConfigInvalid(BaseModel):
    """The run's configuration (``agent.toml`` and what overrides it) is not valid."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["config_invalid"] = "config_invalid"
    problem: _Text
    """The validation's own account (raw text)."""

    def plain(self) -> str:
        """The fact as one plain line."""
        return "the run's configuration is not valid"


class RolesNotDeclared(BaseModel):
    """The configuration names agent roles the run's orchestration does not declare."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["roles_not_declared"] = "roles_not_declared"
    roles: tuple[_Text, ...]

    def plain(self) -> str:
        """The fact as one plain line."""
        return f"the run has no agent role named {', '.join(self.roles)}"


class ModelNotVetted(BaseModel):
    """A model, or none, outside the vetted models of its provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["model_not_vetted"] = "model_not_vetted"
    provider: _Text
    model: _Text | None
    vetted: tuple[_Text, ...]
    role: _Text | None
    """The role whose model it is; ``None`` for the run's own model."""
    fallback: bool
    """Whether it is the quota fallback's model rather than the run's or a role's."""

    def plain(self) -> str:
        """The fact as one plain line."""
        chosen = (
            "no model is named" if self.model is None else f"model {self.model!r} is not vetted"
        )
        return f"{chosen} for provider {self.provider!r}"


class ArgumentsRejected(BaseModel):
    """The run's argument parser refused the extra arguments; ``problem`` is its message (raw)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["arguments_rejected"] = "arguments_rejected"
    problem: _Text

    def plain(self) -> str:
        """The fact as one plain line."""
        return "the extra arguments were not accepted"


class ArgumentsUnrecognized(BaseModel):
    """Extra arguments the run's search does not take, in the order given (raw)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["arguments_unrecognized"] = "arguments_unrecognized"
    arguments: tuple[_Text, ...]

    def plain(self) -> str:
        """The fact as one plain line."""
        return "the extra arguments include some this search does not take"


class RunOption(StrEnum):
    """A typed New run option (``RunOptions``); closed."""

    ROUNDS = "rounds"
    TIME_LIMIT = "time_limit"
    RESULTS = "results"
    ROLE_MODEL = "role_model"
    ROLE_REASONING_EFFORT = "role_reasoning_effort"
    ROLE_AGENT = "role_agent"


class OptionNotAccepted(BaseModel):
    """The run's search does not take a typed option the screen set (for instance rounds)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["option_not_accepted"] = "option_not_accepted"
    option: RunOption

    def plain(self) -> str:
        """The fact as one plain line."""
        return f"this search does not take the {_OPTION_WORDS[self.option]} setting"


class GitHubUnavailable(BaseModel):
    """The run publishes to GitHub, and this machine has no signed-in account; ``problem`` is raw."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["github_unavailable"] = "github_unavailable"
    problem: _Text

    def plain(self) -> str:
        """The fact as one plain line."""
        return "publishing to GitHub needs a signed-in GitHub account, which this machine lacks"


class MeasurementTargetNotConfigured(BaseModel):
    """The cluster is not set up on the machine, or Modal has no token there."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["measurement_target_not_configured"] = "measurement_target_not_configured"
    target: SlurmTarget | ModalTarget

    def plain(self) -> str:
        """The fact as one plain line."""
        if isinstance(self.target, SlurmTarget):
            return f"no Slurm cluster named {self.target.cluster!r} is set up on this machine"
        return "Modal has no token on this machine"


class MeasurementTargetMisfit(BaseModel):
    """The target's hardware cannot run the compute backend the task targets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["measurement_target_misfit"] = "measurement_target_misfit"
    target: MeasurementTarget
    backend: Backend
    """The backend the task targets."""
    offered: Accelerator
    """How the target's GPUs run work (``cpu`` when it has none)."""

    def plain(self) -> str:
        """The fact as one plain line."""
        return (
            f"the task targets the {self.backend.value!r} backend, "
            f"which this {_TARGET_WORDS[self.target.kind]} does not offer"
        )


_TARGET_WORDS = {"same_machine": "machine", "slurm": "cluster", "modal": "cloud"}


Cause = Annotated[
    BackendUnavailable
    | ProjectDirty
    | TaskNotFound
    | TaskUnusable
    | TaskNeedsProvisioning
    | TaskImageUnsupported
    | SysboxMissing
    | ConfigInvalid
    | RolesNotDeclared
    | ModelNotVetted
    | ArgumentsRejected
    | ArgumentsUnrecognized
    | OptionNotAccepted
    | GitHubUnavailable
    | MeasurementTargetNotConfigured
    | MeasurementTargetMisfit,
    Field(discriminator="kind"),
]
"""Why a check failed, as data; ``kind`` discriminates.

Each is a fact the app words for its own controls. A check's ``detail`` says the same in one plain
line that names no flag and carries none of a cause's raw ``problem`` text; the raw text is for a
collapsible detail, never a headline.
"""

_OPTION_WORDS = {
    RunOption.ROUNDS: "rounds",
    RunOption.TIME_LIMIT: "time limit",
    RunOption.RESULTS: "results",
    RunOption.ROLE_MODEL: "role model",
    RunOption.ROLE_REASONING_EFFORT: "role reasoning effort",
}


def backend_unavailable_text(cause: BackendUnavailable) -> str:
    """The fact as one plain line, naming no flag or setting."""
    return (
        f"task {cause.task!r} targets the {cause.backend.value!r} backend, "
        "which this machine cannot run"
    )


def cause_text(cause: Cause) -> str:
    """``cause`` as one plain line that names no flag, setting or raw error text."""
    return cause.plain()


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


class ConfigureCompute(BaseModel):
    """Set up the cluster or Modal on the machine, in that machine's compute settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["configure_compute"] = "configure_compute"
    target: SlurmTarget | ModalTarget


Fix = Annotated[
    ChooseCheckout | CopyOut | EditSetting | StartDocker | DeclareBuild | ConfigureCompute,
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
    cause: Cause | None = None
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
    "TEXT_MAX_CHARACTERS",
    "ArgumentsRejected",
    "ArgumentsUnrecognized",
    "Backend",
    "BackendUnavailable",
    "Cause",
    "Check",
    "CheckId",
    "CheckState",
    "ChooseCheckout",
    "ConfigInvalid",
    "ConfigureCompute",
    "CopyOut",
    "DeclareBuild",
    "EditSetting",
    "Fix",
    "GitHubUnavailable",
    "MeasurementTargetMisfit",
    "MeasurementTargetNotConfigured",
    "ModelNotVetted",
    "OptionNotAccepted",
    "PreflightReport",
    "ProjectDirty",
    "ReasoningEffort",
    "RoleName",
    "RoleOption",
    "RolesNotDeclared",
    "RunOption",
    "RunOptions",
    "RunResults",
    "SettingsInvalid",
    "SettingsValid",
    "StartDocker",
    "SysboxMissing",
    "TaskImageUnsupported",
    "TaskNeedsProvisioning",
    "TaskNotFound",
    "TaskUnusable",
    "backend_unavailable_text",
    "cause_text",
    "clip",
    "one_line",
]
