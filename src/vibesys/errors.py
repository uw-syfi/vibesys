"""Typed product configuration failures shared by public transports.

A configuration failure is a fact first and a sentence second. Each ``ConfigurationCause`` is the
fact a transport can act on without parsing text: the desktop app words it for its own controls,
and the command line words it with the flags it takes (``cli_wording``, the one place that
does). ``configuration_error`` raises a cause as a ``ConfigurationError`` whose message is that
wording, so a run's error line and the fact the app reads cannot disagree.

Public surface: ``ConfigurationDiagnostic``, ``ConfigurationError``, ``ConfigurationCause`` and
its members, ``cli_wording``, ``configuration_error``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class TaskNotFound:
    """The project has no task of that name."""

    code: ClassVar[str] = "invalid_input"
    stage: ClassVar[str] = "input_loading"

    task: str
    available: tuple[str, ...]

    def cli_wording(self) -> str:
        """The command line's message."""
        return (
            f"VibeSys task {self.task!r} was not found. Available tasks: {_names(self.available)}"
        )


@dataclass(frozen=True)
class TaskUnusable:
    """The task's files cannot be loaded; ``problem`` is the loader's own account."""

    code: ClassVar[str] = "invalid_input"
    stage: ClassVar[str] = "input_loading"

    task: str | None
    problem: str

    def cli_wording(self) -> str:
        """The command line's message: the loader's own account."""
        return self.problem


@dataclass(frozen=True)
class TaskNeedsProvisioning:
    """The task's source is provisioned into a copy of the project, which this run does not make."""

    code: ClassVar[str] = "direct_project_materialization_unsupported"
    stage: ClassVar[str] = "input_validation"

    task: str

    def cli_wording(self) -> str:
        """The command line's message, naming the flag that provisions a project."""
        return (
            "A direct project run requires candidate source at the project root. "
            "Use --runs-dir to provision [workspace] inputs into a copied project."
        )


@dataclass(frozen=True)
class TaskImageUnsupported:
    """The task's Dockerfile cannot be honored by the run environment the run would use."""

    code: ClassVar[str] = "task_image_unsupported"
    stage: ClassVar[str] = "run_environment"

    task: str | None
    problem: str
    """The run environment's own account, which names the flags involved."""

    def cli_wording(self) -> str:
        """The command line's message: the run environment's own account."""
        return self.problem


@dataclass(frozen=True)
class ConfigInvalid:
    """The run's configuration (``agent.toml`` and what overrides it) is not valid."""

    code: ClassVar[str] = "config_load_failed"
    stage: ClassVar[str] = "config_loading"

    problem: str

    def cli_wording(self) -> str:
        """The command line's message: the validation's own account."""
        return self.problem


@dataclass(frozen=True)
class RolesNotDeclared:
    """The configuration names agent roles the run's orchestration does not declare."""

    code: ClassVar[str] = "agent_role_configuration_invalid"
    stage: ClassVar[str] = "agent_configuration_validation"

    roles: tuple[str, ...]

    def cli_wording(self) -> str:
        """The command line's message, naming each configuration key."""
        keys = ", ".join(f"agent.roles.{role}" for role in self.roles)
        return f"agent configuration names roles not declared by the selected orchestration: {keys}"


@dataclass(frozen=True)
class ModelNotVetted:
    """A model, or none, outside the vetted models of its provider."""

    code: ClassVar[str] = "agent_harness_unvetted"
    stage: ClassVar[str] = "agent_configuration_validation"

    provider: str
    model: str | None
    vetted: tuple[str, ...]
    role: str | None
    """The role whose model it is; ``None`` for the run's own model."""
    fallback: bool = False
    """Whether it is the quota fallback's model rather than the run's or a role's."""

    def cli_wording(self) -> str:
        """The command line's message, naming the configuration key and the flag that opts out."""
        if self.fallback:
            key = "agent.quota.fallback_model"
        elif self.role is None:
            key = "model.name (or --model)"
        else:
            key = f"agent.roles.{self.role}.model"
        chosen = (
            "no model is named" if self.model is None else f"model {self.model!r} is not vetted"
        )
        return (
            f"{key}: {chosen} for provider {self.provider!r}; vetted models: "
            f"{_names(self.vetted)}. Choose a vetted model, or pass --allow-unvetted-model "
            "at the terminal to run it anyway"
        )


@dataclass(frozen=True)
class ArgumentsRejected:
    """The run's argument parser refused its arguments; ``problem`` is the parser's message."""

    code: ClassVar[str] = "invalid_arguments"
    stage: ClassVar[str] = "argument_parsing"

    problem: str

    def cli_wording(self) -> str:
        """The command line's message: the parser's own."""
        return self.problem


@dataclass(frozen=True)
class ArgumentsUnrecognized:
    """Arguments the run's search does not take, in the order given."""

    code: ClassVar[str] = "invalid_arguments"
    stage: ClassVar[str] = "argument_parsing"

    arguments: tuple[str, ...]

    def cli_wording(self) -> str:
        """The command line's message: the parser's wording for them."""
        return f"unrecognized arguments: {' '.join(self.arguments)}"


@dataclass(frozen=True)
class GitHubUnavailable:
    """Publishing needs a GitHub account the machine does not have; ``problem`` says why."""

    code: ClassVar[str] = "repository_setup_failed"
    stage: ClassVar[str] = "repository_setup"

    problem: str

    def cli_wording(self) -> str:
        """The command line's message: the GitHub client's own account."""
        return self.problem


ConfigurationCause = (
    TaskNotFound
    | TaskUnusable
    | TaskNeedsProvisioning
    | TaskImageUnsupported
    | ConfigInvalid
    | RolesNotDeclared
    | ModelNotVetted
    | ArgumentsRejected
    | ArgumentsUnrecognized
    | GitHubUnavailable
)
"""Every typed configuration failure; closed, so a transport matches it exhaustively."""


@dataclass(frozen=True)
class ConfigurationDiagnostic:
    """Presentation-neutral description of an invalid run configuration."""

    code: str
    stage: str
    message: str
    usage: str | None = None
    exit_code: int = 2
    cause: ConfigurationCause | None = None
    """The failure as data when it has a typed cause; ``message`` is the command line's wording."""


class ConfigurationError(Exception):
    """Raised when user input cannot be resolved into a runnable session."""

    def __init__(self, diagnostic: ConfigurationDiagnostic) -> None:
        """Wrap the configuration diagnostic in an exception."""
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


def cli_wording(cause: ConfigurationCause) -> str:
    """The command line's one message for ``cause``, naming the flags and files it takes."""
    return cause.cli_wording()


def configuration_error(
    cause: ConfigurationCause, *, usage: str | None = None, exit_code: int = 2
) -> ConfigurationError:
    """``cause`` as the error a run raises: its code and stage, its message the CLI's wording."""
    return ConfigurationError(
        ConfigurationDiagnostic(
            code=cause.code,
            stage=cause.stage,
            message=cli_wording(cause),
            usage=usage,
            exit_code=exit_code,
            cause=cause,
        )
    )


def _names(names: tuple[str, ...]) -> str:
    return ", ".join(names) if names else "none"


__all__ = [
    "ArgumentsRejected",
    "ArgumentsUnrecognized",
    "ConfigInvalid",
    "ConfigurationCause",
    "ConfigurationDiagnostic",
    "ConfigurationError",
    "GitHubUnavailable",
    "ModelNotVetted",
    "RolesNotDeclared",
    "TaskImageUnsupported",
    "TaskNeedsProvisioning",
    "TaskNotFound",
    "TaskUnusable",
    "cli_wording",
    "configuration_error",
]
