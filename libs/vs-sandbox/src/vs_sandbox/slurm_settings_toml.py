"""Write parsed Slurm operator settings back out as the operator file's TOML.

The gate wrapper and the evaluation executor load their cluster settings from a
file named by the evaluation plan. A run that holds its settings in memory (for
example the translated deprecated ``slurm-gpu`` file) writes this rendering into
its state directory, so every reader sees exactly the settings the run validated.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_sandbox.slurm_policy import SlurmOperatorSettings


def render_slurm_operator_toml(settings: SlurmOperatorSettings) -> str:
    """Return TOML that ``load_slurm_operator_settings`` parses back to *settings*."""
    document: dict[str, Mapping[str, object]] = {
        "slurm": settings.config.model_dump(mode="json", exclude_none=True),
        "vibesys": settings.policy.model_dump(mode="json", exclude_none=True),
    }
    lines: list[str] = []
    for name, table in document.items():
        _emit_table((name,), table, lines)
    return "\n".join(lines) + "\n"


def _emit_table(path: tuple[str, ...], table: Mapping[str, object], lines: list[str]) -> None:
    if lines:
        lines.append("")
    lines.append(f"[{'.'.join(path)}]")
    nested: list[tuple[str, Mapping[str, object]]] = []
    for key, value in table.items():
        if isinstance(value, dict):
            nested.append((key, value))
        else:
            lines.append(f"{key} = {_value(value)}")
    for key, value in nested:
        _emit_table((*path, key), value, lines)


def _value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        # A JSON string is a valid TOML basic string once DEL, which TOML
        # forbids raw, is escaped; non-ASCII stays literal because JSON would
        # write non-BMP characters as surrogate pairs, which TOML rejects.
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    message = f"cannot render {type(value).__name__} as TOML"
    raise TypeError(message)
