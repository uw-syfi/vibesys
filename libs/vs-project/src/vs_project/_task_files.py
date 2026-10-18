"""Authored task files read by more than the run engine: ``objectives.toml`` and ``[catalogue]``.

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

import tomllib
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    ValidationError,
    model_validator,
)

if TYPE_CHECKING:
    from pathlib import Path

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
