"""Outcome presentation is vocabulary in the schema, never an adapter's lookup table."""

from vs_proto._generated.vibesys.store.v1.lifecycle_pb import (
    Ended,
    Outcome,
    RunOutcomePresentation,
    ext_outcome_presentation,
)


def outcome_presentation(outcome: Outcome) -> RunOutcomePresentation:
    """Return the core vocabulary; reject unknown outcomes with a named validation error."""
    declaration = next(enum for enum in Ended.desc().file.enums if enum.name == "Outcome")
    value = next((value for value in declaration.values if value.number == outcome), None)
    options = None if value is None else value.proto.options
    if options is None or ext_outcome_presentation not in options:
        message = f"ended.outcome: unknown outcome {outcome}"
        raise ValueError(message)
    return RunOutcomePresentation.from_binary(options[ext_outcome_presentation].to_binary())
