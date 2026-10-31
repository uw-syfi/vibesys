"""Synthetic provider transcripts for the usage scan tests.

Written from the format agentshim documents for a Claude session file (one JSON object per line,
an ``assistant`` line carrying a model, a usage, a session, a working directory and the message
and request ids that tell the content blocks of one message apart). Never copied from a real
session: every id, path and count here is made up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from hypothesis import strategies as st

if TYPE_CHECKING:
    from pathlib import Path

START = datetime(2026, 10, 10, 12, tzinfo=UTC)
MODELS = ("claude-opus-5-5", "claude-haiku-5-5", "mystery-model")
CWDS = ("/work/alpha", "/work/beta", "/home/someone/proj")


@dataclass(frozen=True)
class Request:
    """One model request a session made."""

    session: str
    minute: int
    model: str
    cwd: str
    input_tokens: int
    cache_read: int
    output_tokens: int


def line(request: Request, index: int) -> bytes:
    """The transcript line of one request: ``index`` makes its message and request ids."""
    stamp = (START + timedelta(minutes=request.minute)).strftime("%Y-%m-%dT%H:%M:%SZ")
    document = {
        "type": "assistant",
        "timestamp": stamp,
        "sessionId": request.session,
        "cwd": request.cwd,
        "requestId": f"req-{request.session}-{index}",
        "message": {
            "id": f"msg-{request.session}-{index}",
            "model": request.model,
            "usage": {
                "input_tokens": request.input_tokens,
                "cache_read_input_tokens": request.cache_read,
                "cache_creation_input_tokens": 0,
                "output_tokens": request.output_tokens,
            },
        },
    }
    return json.dumps(document).encode() + b"\n"


def session_file(home: Path, session: str) -> Path:
    """Where a Claude session's transcript lives under ``home``."""
    path = home / ".claude" / "projects" / "-work-alpha" / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def requests(
    sessions: tuple[str, ...] = ("s1", "s2", "s3"), *, quarters: int = 12
) -> st.SearchStrategy[Request]:
    """Requests of a few sessions over a few hours."""
    return st.builds(
        Request,
        session=st.sampled_from(sessions),
        minute=st.integers(min_value=0, max_value=15 * quarters),
        model=st.sampled_from(MODELS),
        cwd=st.sampled_from(CWDS),
        input_tokens=st.integers(min_value=0, max_value=100_000),
        cache_read=st.integers(min_value=0, max_value=50_000),
        output_tokens=st.integers(min_value=0, max_value=10_000),
    )
