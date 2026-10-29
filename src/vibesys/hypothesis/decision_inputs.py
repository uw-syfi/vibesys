"""The inputs a decision field of an agent's reply is decided from.

A reply field that asks the agent to choose (a status, a verdict, a bar, a base) is only as
good as the facts the agent is shown for it: asking an agent to decide from facts its prompt
does not carry is the failure class this module makes checkable. A reply model marks such a
field with ``DecidedFrom`` in its ``Annotated`` type; the marker is kept on the field and never
reaches the schema the agent receives.

An input is the dotted path of a field of the prompt context that role is asked with
(``plan.pass_criteria`` is ``pass_criteria`` of the context's ``plan``). Naming the path, not
prose, lets a test resolve it against the context type and find it in the rendered prompt
(``tests/architecture/test_decision_inputs.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import BaseModel


@dataclass(frozen=True, init=False)
class DecidedFrom:
    """Marks a reply field as a decision made from the named prompt context fields."""

    inputs: tuple[str, ...]

    def __init__(self, *inputs: str) -> None:
        """Name at least one distinct input, each a dotted path into the prompt context."""
        if not inputs:
            message = "a decision field must name at least one input"
            raise ValueError(message)
        if len(set(inputs)) != len(inputs):
            message = f"decision inputs repeat a path: {inputs}"
            raise ValueError(message)
        object.__setattr__(self, "inputs", inputs)


def declared_inputs(model: type[BaseModel]) -> dict[str, tuple[str, ...]]:
    """The inputs each decision field of ``model`` declares, by field name."""
    return {
        name: marker.inputs
        for name, field in model.model_fields.items()
        for marker in field.metadata
        if isinstance(marker, DecidedFrom)
    }
