"""Run notes shared with the TUI (``$VIBESYS_STATE_HOME/tui/notes/<run>.json``, last write wins)."""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING

from pydantic import ValidationError

from entrypoints.web_home.context import atomic_write, parse_body, safe_segment
from entrypoints.web_home.contract import ApiError, ErrorCode, NoteRecord, NoteResponse, NoteUpdate

if TYPE_CHECKING:
    from pathlib import Path

    from entrypoints.web_home.context import HomeConfig, Request

_MAX_RUN_ID = 256


def note_path(config: HomeConfig, run_id: str) -> Path:
    """Return the note file the TUI's ``notePath`` names for *run_id*."""
    if not run_id or len(run_id) > _MAX_RUN_ID:
        message = "run id must be 1 to 256 characters"
        raise ApiError(ErrorCode.INVALID_REQUEST, message)
    return config.state_home / "tui" / "notes" / f"{safe_segment(run_id)}.json"


def _read(path: Path) -> NoteRecord | None:
    try:
        return NoteRecord.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        return None


def get_note(request: Request) -> NoteResponse:
    """``GET /api/notes/{run}``: the note, or ``null`` when absent or unreadable."""
    return NoteResponse(note=_read(note_path(request.config, request.params[0])))


def put_note(request: Request) -> NoteResponse:
    """``PUT /api/notes/{run}``: replace the note text, keeping its creation time."""
    body = parse_body(request, NoteUpdate)
    config = request.config
    run_id = request.params[0]
    path = note_path(config, run_id)
    now = config.clock().astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with config.write_lock:
        existing = _read(path)
        note = NoteRecord(
            run_id=run_id,
            text=body.text,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )
        atomic_write(
            path, note.model_dump_json(by_alias=True, indent=2).encode("utf-8"), mode=0o600
        )
    return NoteResponse(note=note)
