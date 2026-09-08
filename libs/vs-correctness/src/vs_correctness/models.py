"""Serializable contracts for microservice correctness evaluation."""

from __future__ import annotations

import json
import urllib.parse
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


class StrictModel(BaseModel):
    """A persisted contract that rejects misspelled fields."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Reference(StrictModel):
    """A value captured from an earlier result in the same environment."""

    kind: Literal["reference"] = "reference"
    phase: Literal["setup", "actions"] = "actions"
    index: int = Field(ge=0)
    source: Literal["status", "header", "json"]
    header: str | None = None
    json_path: tuple[str | int, ...] = ()


ScalarInput = str | Reference


class HTTPAction(StrictModel):
    """One declarative HTTP request."""

    kind: Literal["http"] = "http"
    method: str
    path: str
    query: dict[str, ScalarInput | list[ScalarInput]] = Field(default_factory=dict)
    headers: dict[str, ScalarInput] = Field(default_factory=dict)
    body: Reference | JsonValue | str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        """Keep every request inside its selected environment."""
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme or parsed.netloc or not value.startswith("/"):
            raise ValueError(  # noqa: TRY003
                "HTTP action path must be absolute-path relative, such as '/users'"
            )
        return value


class CustomAction(StrictModel):
    """Serializable action interpreted by a user-supplied executor."""

    kind: Literal["custom"] = "custom"
    name: str
    payload: JsonValue = None


Action = Annotated[HTTPAction | CustomAction, Field(discriminator="kind")]


class TestCase(StrictModel):
    """Replayable executable inputs, separate from any target environment."""

    id: str
    setup: tuple[Action, ...] = ()
    actions: tuple[Action, ...]
    cleanup: tuple[Action, ...] = ()


class Environment(StrictModel):
    """One execution target and its reportable identity."""

    name: str
    base_url: str
    revision: str | None = None
    config: dict[str, JsonValue] = Field(default_factory=dict)


class ActionResult(StrictModel):
    """Normalized result of one action."""

    phase: Literal["setup", "actions", "cleanup"]
    index: int
    status: int | None = None
    headers: dict[str, list[str]] = Field(default_factory=dict)
    body: str = ""
    elapsed_seconds: float = 0
    error: str | None = None

    def json_body(self) -> JsonValue:
        """Decode the response body as JSON."""
        return json.loads(self.body)


class Observation(StrictModel):
    """All externally visible results from one environment."""

    environment: Environment
    action_results: tuple[ActionResult, ...]
    artifacts: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def failed(self) -> bool:
        """Whether execution encountered a transport or framework failure."""
        return any(result.error is not None for result in self.action_results)


class Verdict(StrEnum):
    """A user oracle's conclusion."""

    PASS = "pass"  # noqa: S105
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class Decision(StrictModel):
    """One oracle verdict with an actionable reason."""

    verdict: Verdict
    reason: str = ""


class OracleContext(StrictModel):
    """Inputs available to absolute and differential oracles."""

    test_case: TestCase
    candidate: Observation
    baseline: Observation | None = None


class CaseResult(StrictModel):
    """Reported outcome for one generated or replayed case."""

    test_case: TestCase
    decision: Decision
    candidate: Observation
    baseline: Observation | None = None
    minimized_case: TestCase | None = None
    shrink_attempts: int = 0


class VerificationReport(StrictModel):
    """Complete, replayable output of a verification run."""

    schema_version: Literal[2] = 2
    seed: int
    candidate: Environment
    baseline: Environment | None = None
    results: tuple[CaseResult, ...]

    @property
    def passed(self) -> bool:
        """Require a nonempty suite in which every case passed."""
        return bool(self.results) and all(
            result.decision.verdict is Verdict.PASS for result in self.results
        )
