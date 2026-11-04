"""The evaluation ledger's format 1, frozen as builds before #3173 read it (main at e9f6f709e).

Those builds decode every ``attempt-*.json`` revision of a request, and every
lease, with these models, and fail the whole request on the first document
they cannot decode. So every record and lease a newer build writes must decode
here, or an older build sharing the project's ledger can no longer evaluate
that request. Never edit these models: they stand for code that is already
deployed. A deliberate format change bumps ``format_version`` and keeps the
documents an older build reads in this format.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

_VERSION = 1


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Artifact(_Frozen):
    remote_path: str
    kind: Literal["file", "tree"]
    collect_on_failure: bool = False


class _Stage(_Frozen):
    name: str
    artifacts: tuple[_Artifact, ...] = ()


class _Job(_Frozen):
    kind: Literal["submit_bound"] = "submit_bound"
    job_id: Annotated[str, Field(pattern=r"^[0-9]+$", max_length=32)]
    config_identity: str
    remote_workspace: str
    remote_status_path: str
    remote_log_path: str
    stop_on_failure: bool
    stages: tuple[_Stage, ...] = Field(min_length=1)
    result_layout: int = 1


class FormatOneState(StrEnum):
    """The states format 1 has words for."""

    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    FAILED = "failed"
    WITHDRAWN = "withdrawn"


class _Labels(_Frozen):
    candidate: str | None = None
    workstream: str | None = None


class FormatOneRecord(_Frozen):
    """A record revision as format 1 reads it."""

    format_version: Literal[1] = 1
    key: str
    project: str
    attempt: int = Field(ge=0)
    operation_id: str
    owner: str
    created_at: float
    claim_expires_at: float
    labels: _Labels = _Labels()
    state: FormatOneState
    job: _Job | None = None
    reason: str | None = None


class FormatOneLease(_Frozen):
    """A lease as format 1 reads it."""

    format_version: Literal[1] = 1
    holder: str
    renewed_at: float
    expires_at: float


_OBJECT = TypeAdapter(dict[str, object])


def read_as_format_1[T: FormatOneRecord | FormatOneLease](text: str, model: type[T]) -> T:
    """Decode ``text`` as the older build does: any version but 1, or any unknown field, fails."""
    document = _OBJECT.validate_json(text)
    if document.get("format_version") != _VERSION:
        message = f"unsupported format_version {document.get('format_version')!r}"
        raise ValueError(message)
    return model.model_validate(document, strict=False)


__all__ = ["FormatOneLease", "FormatOneRecord", "FormatOneState", "read_as_format_1"]
