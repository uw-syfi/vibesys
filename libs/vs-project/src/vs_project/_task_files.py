"""Authored task files read by more than the run engine: ``objectives.toml``, ``[catalogue]`` and ``[[comparison]]``.

Each is parsed once, here, through models that forbid unknown keys. A misspelled key must
fail by name: the only sanctioned zero tolerance is an absent ``[pareto]`` table, and an
unrecognized key must not reproduce it, or an empty axis list, silently.

``objectives.toml`` holds the axes and the measurement tolerance. The ``[catalogue]`` table of
``vibesys.input.toml`` says what an example task is about (its domain, served model, target
hardware and workload) so a client can offer it without reading a directory name. The run
engine validates the table as part of the input manifest; the example catalogue reads it
alone, without loading the engine.
"""

from __future__ import annotations

import re
import tomllib
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from vs_project._paths import STATE_DIRECTORY_POSIX
from vs_project._state import StateNamespace

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project._state import ProjectState

OBJECTIVES_NAME = "objectives.toml"


class ObjectiveAxisInput(BaseModel):
    """One ``[[objective]]`` entry: a named metric and which way is better."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    direction: Literal["max", "min"]


class ParetoInput(BaseModel):
    """The ``[pareto]`` table: the workload's declared measurement variation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_noise: Annotated[StrictInt | StrictFloat, Field(ge=0, lt=1)] = 0


class ObjectivesInput(BaseModel):
    """Parsed ``objectives.toml``. The default is the file being absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: tuple[ObjectiveAxisInput, ...] = ()
    pareto: ParetoInput = ParetoInput()


def _key_path(location: tuple[int | str, ...]) -> str:
    path = ""
    for part in location:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path.removeprefix(".")


def load_objectives(task_root: Path) -> ObjectivesInput:
    """Read and validate ``objectives.toml`` under *task_root*.

    An absent file is the default value. Every defect raises ``ValueError``
    naming the file and the offending key path, such as ``pareto.relative_noise``.
    """
    path = task_root / OBJECTIVES_NAME
    if not path.exists():
        return ObjectivesInput()
    try:
        return ObjectivesInput.model_validate(tomllib.loads(path.read_text()))
    except tomllib.TOMLDecodeError as exc:
        message = f"Malformed {path}: {exc}"
        raise ValueError(message) from exc
    except ValidationError as exc:
        problems = "; ".join(
            f"{_key_path(error['loc']) or '<file>'}: {error['msg']} (got {error['input']!r})"
            for error in exc.errors()
        )
        message = (
            f"Invalid {path}: {problems}. objectives.toml accepts only [[objective]] "
            "(name, direction) and [pareto] (relative_noise); remove or correct other keys."
        )
        raise ValueError(message) from exc


class ExampleDomain(StrEnum):
    """What an example task optimizes; the first question New run asks."""

    LLM_SERVING = "llm-serving"
    DATA_STRUCTURES = "data-structures"
    DATABASES = "databases"
    MICROSERVICES = "microservices"


class ModelMaker(StrEnum):
    """Who made a served model; ``OTHER`` for a maker with no entry yet."""

    META = "meta"
    QWEN = "qwen"
    DEEPSEEK = "deepseek"
    ZHIPU = "zhipu"
    MOONSHOT = "moonshot"
    AI2 = "ai2"
    OPENAI = "openai"
    OTHER = "other"


class AcceleratorVendor(StrEnum):
    """Who makes the hardware a task targets."""

    NVIDIA = "nvidia"
    AMD = "amd"
    AWS = "aws"
    APPLE = "apple"
    CPU = "cpu"


class ServedModelInput(BaseModel):
    """The model an LLM-serving task serves."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=80)
    """The display name, for example ``Llama 3.3 70B``."""
    maker: ModelMaker


class HardwareInput(BaseModel):
    """The hardware a task targets: ``count`` of ``accelerator``, as a person would say it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accelerator: str = Field(min_length=1, max_length=80)
    """The display name, for example ``NVIDIA H100`` or ``CPU``."""
    count: int = Field(ge=1, le=4096)
    vendor: AcceleratorVendor


class WorkloadInput(BaseModel):
    """The workload a data-structure task drives."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=400)


class CatalogueInput(BaseModel):
    """The ``[catalogue]`` table of ``vibesys.input.toml``.

    The compute backend is not repeated here: it is the first of the file's ``backends``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: ExampleDomain
    hardware: HardwareInput
    model: ServedModelInput | None = None
    """Required for, and only for, ``llm-serving``."""
    workload: WorkloadInput | None = None
    """Required for, and only for, ``data-structures``."""

    @model_validator(mode="after")
    def _facts_match_the_domain(self) -> Self:
        problems: list[str] = []
        serving = self.domain is ExampleDomain.LLM_SERVING
        structures = self.domain is ExampleDomain.DATA_STRUCTURES
        if serving != (self.model is not None):
            problems.append("model is required for llm-serving and only for it")
        if structures != (self.workload is not None):
            problems.append("workload is required for data-structures and only for it")
        if problems:
            message = f"domain {self.domain.value}: " + "; ".join(problems)
            raise ValueError(message)
        return self


# --- [[comparison]]: comparison baselines (see docs/running-vibesys.md) ---

PORT_PLACEHOLDER = "${PORT}"
"""The one placeholder ``serve`` may use; the measurement substitutes the port it assigns."""

_NAME = r"^[a-z][a-z0-9_-]{0,31}$"
_IMAGE = r"^docker://[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}$"
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")

DEFAULT_READY_TIMEOUT_SECONDS = 1800
"""How long the measurement waits for the server to answer: engines compile on first start."""

ComparisonName = Annotated[str, Field(pattern=_NAME)]


class ComparisonInput(BaseModel):
    """One ``[[comparison]]`` table: an engine to measure beside the input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: ComparisonName
    """The comparison's id: lowercase, used in cache keys, events and ``--only``-style selection."""
    label: str = Field(min_length=1, max_length=40)
    """The words a client shows after "vs", for example ``vLLM``."""
    image: str | None = Field(default=None, pattern=_IMAGE)
    """A published image pinned by digest, ``docker://repo@sha256:<64 hex>``; a tag is refused."""
    dockerfile: str | None = None
    """A Dockerfile path relative to the task, built and tagged by the hash of its build context."""
    serve: tuple[str, ...] = Field(min_length=1)
    """The server's argv, run inside the image; ``${PORT}`` is replaced by the assigned port."""
    env: dict[str, str] = Field(default_factory=dict)
    """Environment variables for the server; never credentials (those are per-machine)."""
    benchmark_args: tuple[str, ...] = ()
    """Arguments appended to the task's ``[benchmark].args`` when measuring this comparison."""
    ready_timeout_seconds: int = Field(default=DEFAULT_READY_TIMEOUT_SECONDS, gt=0)
    visible_to_agents: StrictBool = False
    """Whether the planner is told this comparison's name and number; hidden unless set."""

    @field_validator("label")
    @classmethod
    def _label_is_one_printable_line(cls, value: str) -> str:
        if not value.isprintable() or value != value.strip():
            message = "label must be one printable line without leading or trailing space"
            raise ValueError(message)
        return value

    @field_validator("dockerfile")
    @classmethod
    def _dockerfile_stays_in_the_task(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = PurePosixPath(value)
        if not value.strip() or path.is_absolute() or ".." in path.parts:
            message = f"dockerfile must be a path inside the task, relative to it: {value!r}"
            raise ValueError(message)
        return value

    @field_validator("serve", "benchmark_args")
    @classmethod
    def _argv_elements_are_non_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not part for part in value):
            message = "argv elements must be non-empty strings"
            raise ValueError(message)
        return value

    @field_validator("env")
    @classmethod
    def _env_keys_are_names(cls, value: dict[str, str]) -> dict[str, str]:
        bad = sorted(key for key in value if not _ENV_KEY.fullmatch(key))
        if bad:
            message = f"env keys must be environment variable names: {', '.join(map(repr, bad))}"
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def _declared_consistently(self) -> Self:
        problems: list[str] = []
        sources = [key for key in ("image", "dockerfile") if getattr(self, key) is not None]
        if len(sources) != 1:
            problems.append(
                "declare exactly one of image (a docker:// reference pinned by @sha256: digest) "
                f"or dockerfile (a path in the task); got {'both' if sources else 'neither'}. "
                "A comparison never uses the task's or the agent's image"
            )
        joined = "\0".join(self.serve)
        unknown = sorted({m for m in _PLACEHOLDER.findall(joined) if m != "PORT"})
        if unknown:
            problems.append(
                f"serve may only use the placeholder {PORT_PLACEHOLDER}; "
                f"got {', '.join('${' + name + '}' for name in unknown)}"
            )
        if PORT_PLACEHOLDER not in joined:
            problems.append(f"serve must contain {PORT_PLACEHOLDER} so the server takes its port")
        if problems:
            message = f"comparison {self.name!r}: " + "; ".join(problems)
            raise ValueError(message)
        return self

    def serve_argv(self, port: int) -> tuple[str, ...]:
        """The server argv with ``${PORT}`` replaced by *port*."""
        return tuple(part.replace(PORT_PLACEHOLDER, str(port)) for part in self.serve)


def check_comparisons_are_distinct(comparisons: tuple[ComparisonInput, ...]) -> None:
    """Raise ``ValueError`` naming a repeated comparison name or label."""
    for key in ("name", "label"):
        seen: set[str] = set()
        for comparison in comparisons:
            value: str = getattr(comparison, key)
            if value in seen:
                message = f"comparison {key} {value!r} is declared twice; each must be distinct"
                raise ValueError(message)
            seen.add(value)


# --- where comparison readings are kept ---

_NAME = "comparisons"


def comparison_directory(state: ProjectState) -> Path:
    """The machine-local directory holding this project's comparison readings.

    It lives with the project's other machine-local caches: never committed, and below the
    ``local`` state the agent cannot see.
    """
    return state.model_cache_directory(_NAME)


def comparison_namespace(state: ProjectState) -> StateNamespace:
    """The machine-local namespace for the cluster jobs' records of measuring comparisons."""
    directory = comparison_directory(state)
    (directory / "evaluations").mkdir(parents=True, exist_ok=True)
    return StateNamespace(
        project_root=state.project_root,
        root=directory / "evaluations",
        portable=False,
        containment_root=directory,
        namespace_root=STATE_DIRECTORY_POSIX / "local" / _NAME / PurePosixPath("evaluations"),
    )
