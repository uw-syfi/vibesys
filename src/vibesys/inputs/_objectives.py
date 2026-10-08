"""The ``objectives.toml`` file of an input bundle: axes and measurement tolerance.

The file is authored input, so it is parsed once, here, through models that
forbid unknown keys. A misspelled key must fail by name: the only sanctioned
zero tolerance is an absent ``[pareto]`` table, and an unrecognized key must
not reproduce it, or an empty axis list, silently.
"""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, ValidationError

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
        message = f"Invalid {path}: {problems}"
        raise ValueError(message) from exc
