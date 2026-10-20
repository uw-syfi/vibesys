"""The ``[[comparison]]`` tables of ``vibesys.input.toml``: comparison baselines.

A comparison baseline is a mature engine (vLLM, SGLang) that a run measures with the same
benchmark as its candidates and shows next to the input's reading. It is never a candidate:
the model here only says *what* to serve and *how*; measuring it, caching the reading and
showing it belong to the layers above. Machine-specific settings (an image cache directory,
credentials) are never part of a task, so none appear here.

The engine's software comes from the comparison's own image, either a published one pinned
by digest or one built from a Dockerfile in the task. A comparison never falls back to the
task's or the agent's image, so declaring neither or both sources is an error that names the
comparison.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

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
