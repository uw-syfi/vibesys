"""Export pre-existing public constants from their authoritative proto constraints."""

from __future__ import annotations

import re

from scripts.corevalue._schema import GenerationError, extension, options, rule_of, rules


def constants(file: object) -> list[tuple[str, str]]:
    """Return Python names and literal values, with no handwritten copy of a constraint."""
    result = []
    for message in file.messages:
        config = extension(message, options.ext_core_value)
        checks = extension(message, rules.ext_message)
        for declaration in config.constants if config else ():
            rule = (
                next((r for r in checks.cel if r.id == declaration.rule), None) if checks else None
            )
            match = re.fullmatch(r"this\.\w+\s*<=\s*([0-9]+)", rule.expression) if rule else None
            if match is None:
                raise GenerationError(
                    message.type_name,
                    f"{declaration.name}: constant must name a scalar upper-bound CEL rule",
                )
            result.append((declaration.name, match[1]))
        for field in message.fields:
            exported = _field_constant(field)
            if exported is not None:
                result.append(exported)
    return result


def _field_constant(field: object) -> tuple[str, str] | None:
    config = extension(field, options.ext_core_field)
    if not config or not config.constant_name:
        return None
    rule = rule_of(field)
    if not rule or not rule.type or not hasattr(rule.type.value, config.constant_rule):
        raise GenerationError(
            field.parent.type_name, f"{field.name}: constant must name a scalar rule"
        )
    value = getattr(rule.type.value, config.constant_rule)
    return config.constant_name, repr(value)
