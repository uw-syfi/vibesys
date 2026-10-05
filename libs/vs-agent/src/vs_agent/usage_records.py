"""The per-turn usage record: one JSON line per dispatched agent turn in ``usage.jsonl``.

The production client and the Fake client both write it through this module, so a test
that reads ``usage.jsonl`` sees the rows a real run writes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from vs_agent.contracts import AgentSkillUse, AgentUsage

USAGE_FILE = "usage.jsonl"


def usage_dict(usage: AgentUsage) -> dict[str, int | float | None]:
    """The token and cost fields of one turn's usage."""
    return {
        "input_tokens": usage.input_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "output_tokens": usage.output_tokens,
        "total_cost_usd": usage.total_cost_usd,
        "duration_ms": usage.duration_ms,
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
) -> None:
    """Append one turn's row to ``log_dir``'s usage file; raise ``OSError`` if it cannot."""
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "kind": kind,
        "round_label": round_label,
        "provider": provider,
        "model": model,
        "reasoning_effort": reasoning_effort,
        **usage_dict(usage),
        **skill_dict(skills),
    }
    with (log_dir / USAGE_FILE).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
