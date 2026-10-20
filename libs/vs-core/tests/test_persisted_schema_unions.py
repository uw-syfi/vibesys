"""The persisted-shape check holds unions, their discriminators and nested classes.

A discriminated union decodes a document by the value of one property (its discriminator), so a
change of that property, or of the class a tag selects, breaks every saved document of the
union under an unchanged version, even when each class on its own would still take them.
"""

from __future__ import annotations

from vs_core.api import ENVELOPE_SCHEMA_VERSION, FlowState, RunEnvelope
from vs_core.api.testing import schema_snapshot, snapshot_problems


def _envelope_snapshot() -> str:
    schema = RunEnvelope[FlowState].model_json_schema()
    return schema_snapshot("RunEnvelope[FlowState]", (ENVELOPE_SCHEMA_VERSION,), schema)


def test_a_changed_discriminator_of_the_real_envelope_is_reported() -> None:
    """Regression: renaming the envelope's first discriminator property was accepted."""
    base = _envelope_snapshot()
    assert '"propertyName":"kind"' in base
    head = base.replace('"propertyName":"kind"', '"propertyName":"variant"', 1)
    problems = snapshot_problems(base, head, "envelope.persisted-schema.txt")
    assert any("discriminator" in problem for problem in problems), problems
