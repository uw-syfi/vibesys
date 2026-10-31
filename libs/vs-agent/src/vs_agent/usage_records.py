"""The per-turn usage record: one JSON line per dispatched agent turn in ``usage.jsonl``.

The production client and the Fake client both write it through this module, so a test
that reads ``usage.jsonl`` sees the rows a real run writes.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from vs_agent.contracts import AgentModelUsage, AgentSkillUse, AgentUsage

USAGE_FILE = "usage.jsonl"


def usage_dict(usage: AgentUsage) -> dict[str, int | float | None]:
    """The token and cost fields of one turn's usage."""
    return {
        "input_tokens": usage.input_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_output_tokens": usage.reasoning_output_tokens,
        "total_cost_usd": usage.total_cost_usd,
        "duration_ms": usage.duration_ms,
    }


def model_usage_dict(row: AgentModelUsage) -> dict[str, str | int | float | bool | None]:
    """One model's share of a turn, as ``usage.jsonl`` and the journal's ``by_model`` name it."""
    return {
        "model": row.model,
        "input_tokens": row.input_tokens,
        "cache_creation_input_tokens": row.cache_creation_input_tokens,
        "cache_creation_1h_input_tokens": row.cache_creation_1h_input_tokens,
        "cache_read_input_tokens": row.cache_read_input_tokens,
        "output_tokens": row.output_tokens,
        "reasoning_output_tokens": row.reasoning_output_tokens,
        "turns": row.turns,
        "recorded_usd": row.recorded_usd,
        "speed": None if row.speed is None else row.speed.value,
        "primary": row.primary,
    }


def skill_dict(skills: AgentSkillUse) -> dict[str, int | list[str] | None]:
    """Usage-record fields for skill use; ``None`` where the provider cannot say."""
    invoked = skills.invoked
    return {
        "skill_uses": None if invoked is None else len(invoked),
        "skills_invoked": None if invoked is None else list(invoked),
        "skills_offered": None if skills.offered is None else len(skills.offered),
    }


def append_usage_record(  # noqa: PLR0913  # lint-waiver: LW-135801 [PLR0913]; the record's fields are independent facts of one turn, named at the call site.
    log_dir: Path,
    *,
    kind: str,
    round_label: str | None,
    provider: str | None,
    model: str | None,
    reasoning_effort: str | None,
    usage: AgentUsage,
    skills: AgentSkillUse,
    recorded_at: datetime,
    provider_session_id: str | None = None,
) -> None:
    """Append one turn's row to ``log_dir``'s usage file; raise ``OSError`` if it cannot.

    ``cost_basis`` and ``by_model`` are the turn's cost provenance and per-model split;
    ``provider_session_id`` names the conversation the provider ran the turn in, ``None`` when it
    named none (or the turn ended before it did).
    """
    record = {
        "timestamp": recorded_at.isoformat(),
        "kind": kind,
        "round_label": round_label,
        "provider": provider,
        "model": model,
        "reasoning_effort": reasoning_effort,
        **usage_dict(usage),
        "cost_basis": None if usage.cost_basis is None else usage.cost_basis.value,
        "by_model": [model_usage_dict(row) for row in usage.by_model],
        "provider_session_id": provider_session_id,
        **skill_dict(skills),
    }
    with (log_dir / USAGE_FILE).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
