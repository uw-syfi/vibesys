"""The reply an agent turn may end with instead of its result: a wait for its evaluations.

A turn that submitted measurements through the evaluation tool may end by naming their handles
with ``waiting_for_evaluation``; the run resumes the turn once when they settle. Every role that
can submit a measurement answers with a *result or a wait*, so the union and its discriminator
are one shared contract, not one per strategy. The bridge validates the handles against the
scope's own admitted submissions (`AgentEvaluationBridge`); this module owns only the wire form.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self, override

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    field_validator,
)
from pydantic.json_schema import GenerateJsonSchema


class WaitingForDependency(BaseModel):
    """End this agent turn until every owned evaluation handle settles."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    handles: tuple[str, ...] = Field(min_length=1)

    @field_validator("handles")
    @classmethod
    def _unique_nonempty_handles(cls, handles: tuple[str, ...]) -> tuple[str, ...]:
        if any(not handle.strip() or handle != handle.strip() for handle in handles) or len(
            handles
        ) != len(set(handles)):
            message = "handles must be nonblank and unique"
            raise ValueError(message)
        return handles


class WaitingForEvaluation(WaitingForDependency):
    """End this agent turn until every owned evaluation handle settles."""

    kind: Literal["waiting_for_evaluation"]


def _reply_kind(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("kind", "result"))
    return str(getattr(value, "kind", "result"))


type Waitable[R] = Annotated[
    Annotated[R, Tag("result")] | Annotated[WaitingForEvaluation, Tag("waiting_for_evaluation")],
    Discriminator(_reply_kind),
]
"""A reply that is either the role's result ``R`` or a wait for evaluations.

A result carries no ``kind`` of its own (or any value other than ``waiting_for_evaluation``).
"""


def _use_any_of(value: object) -> None:
    """Rewrite Pydantic's tagged unions into the supported JSON Schema form."""
    if isinstance(value, dict):
        members = value.pop("oneOf", None)
        if members is not None:
            value["anyOf"] = members
        for child in value.values():
            _use_any_of(child)
    elif isinstance(value, list):
        for child in value:
            _use_any_of(child)


class WaitableReply[R](BaseModel):
    """Object-rooted agent wire envelope for a result or evaluation wait.

    Native provider schemas require an object at the root and reject a root
    union. The union therefore lives under ``reply``. Validation also accepts
    the historical bare result/wait form so an in-flight turn from an older
    process can settle after an upgrade; serialization always emits the
    canonical envelope.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reply: Waitable[R]

    @classmethod
    @override
    def model_validate(
        cls,
        obj: Any,
        *,
        strict: bool | None = None,
        extra: Literal["allow", "ignore", "forbid"] | None = None,
        from_attributes: bool | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """Validate canonical Python input first, then the historical bare shape."""
        try:
            return super().model_validate(
                obj,
                strict=strict,
                extra=extra,
                from_attributes=from_attributes,
                context=context,
                by_alias=by_alias,
                by_name=by_name,
            )
        except ValidationError as canonical_error:
            try:
                return super().model_validate(
                    {"reply": obj},
                    strict=strict,
                    extra=extra,
                    from_attributes=from_attributes,
                    context=context,
                    by_alias=by_alias,
                    by_name=by_name,
                )
            except ValidationError:
                raise canonical_error from None

    @classmethod
    @override
    def model_validate_json(
        cls,
        json_data: Any,
        *,
        strict: bool | None = None,
        extra: Literal["allow", "ignore", "forbid"] | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """Validate canonical JSON first, then the historical bare JSON shape."""
        try:
            return super().model_validate_json(
                json_data,
                strict=strict,
                extra=extra,
                context=context,
                by_alias=by_alias,
                by_name=by_name,
            )
        except ValidationError as canonical_error:
            raw = json_data.encode() if isinstance(json_data, str) else bytes(json_data)
            try:
                return super().model_validate_json(
                    b'{"reply":' + raw + b"}",
                    strict=strict,
                    extra=extra,
                    context=context,
                    by_alias=by_alias,
                    by_name=by_name,
                )
            except ValidationError:
                raise canonical_error from None

    @classmethod
    @override
    def model_json_schema(
        cls,
        by_alias: bool = True,
        ref_template: str = "#/$defs/{model}",
        schema_generator: type[GenerateJsonSchema] = GenerateJsonSchema,
        mode: Literal["validation", "serialization"] = "validation",
        *,
        union_format: Literal["any_of", "primitive_type_array"] = "any_of",
    ) -> dict[str, object]:
        """Expose an object root and a nested ``anyOf`` result/wait choice."""
        schema = super().model_json_schema(
            by_alias=by_alias,
            ref_template=ref_template,
            schema_generator=schema_generator,
            mode=mode,
            union_format=union_format,
        )
        _use_any_of(schema)
        return schema
