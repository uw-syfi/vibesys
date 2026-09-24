"""Shared plumbing for golden snapshots of agent-facing prompts, progress-board
files, and core events.

These snapshots exist to prove behavior preservation across the
``orchestration-simplify`` refactor: capture exactly what an agent reads and
exactly what the workspace/event stream look like after a scripted round,
before the loops/ layers move, then diff the same capture after each phase.

Every snapshot is keyed by ``(strategy, role_or_file, scenario)``, never by an
internal function or class name, so the fixture survives the refactor.

Regenerate with ``UPDATE_GOLDEN=1 uv run pytest tests/vibesys/golden``.
Review every fixture diff as a prompt/board/event diff -- do not blindly
accept a regenerated snapshot.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from vs_agent.api.testing import FakeAgentClient

_SNAPSHOT_ROOT = Path(__file__).with_name("snapshots")

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\b"
)
_RUN_ID_RE = re.compile(r"\b\d{8}-\d{6}-[0-9a-f]{8}-[A-Za-z0-9_-]+\b")
_COMPACT_TIMESTAMP_RE = re.compile(r"\b\d{8}-\d{6}\b")
# pytest numbers each test's directory (``test_x0``, ``test_x1``) by how many
# same-named tests ran earlier in the session, so the suffix depends on
# collection order; ``.vibesys-state-`` is the sibling state home conftest sets.
_TEST_DIR_RE = re.compile(r"(<WORKSPACE>/(?:\.vibesys-state-)?)\w*?\d+(?!\w)")
_DURATION_RE = re.compile(r'"duration_(?:ms|seconds)":\s*[0-9.]+')
_FREE_TEXT_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|seconds)\b")


def normalize_text(text: str, *, workspace: Path | None = None) -> str:
    """Replace nondeterministic substrings with stable placeholders.

    Order matters: the run-id pattern is more specific than the bare SHA
    pattern and must run first, and the workspace path (if given) is
    replaced before generic hex substrings so a path component made only of
    hex characters doesn't first get eaten by ``_SHA_RE``.
    """
    normalized = text
    if workspace is not None:
        normalized = normalized.replace(str(workspace), "<WORKSPACE>")
        resolved = str(workspace.resolve())
        if resolved != str(workspace):
            normalized = normalized.replace(resolved, "<WORKSPACE>")
        normalized = _TEST_DIR_RE.sub(r"\1<TEST_DIR>", normalized)
    normalized = _RUN_ID_RE.sub("<RUN_ID>", normalized)
    normalized = _COMPACT_TIMESTAMP_RE.sub("<TIMESTAMP>", normalized)
    normalized = _TIMESTAMP_RE.sub("<TIMESTAMP>", normalized)
    normalized = _UUID_RE.sub("<UUID>", normalized)
    normalized = _SHA_RE.sub(_normalize_hex_token, normalized)
    normalized = _DURATION_RE.sub('"duration": "<DURATION>"', normalized)
    return _FREE_TEXT_DURATION_RE.sub("<DURATION>", normalized)


def _normalize_hex_token(match: re.Match[str]) -> str:
    """Only fold a hex-looking token to a placeholder when it mixes digits
    and letters or is long enough to be a commit SHA; a bare short number
    (a round number, a retry count) must survive normalization untouched.
    """
    token = match.group(0)
    if len(token) >= 12:  # tracked: #288
        return "<SHA>"
    if token.isdigit():
        return token
    has_letter = any(char.isalpha() for char in token)
    return "<SHA>" if has_letter else token


def _assert_matches_snapshot(path: Path, rendered: str) -> None:
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return
    if not path.exists():
        pytest.fail(
            f"Missing golden snapshot: {path}\n"
            "Regenerate with UPDATE_GOLDEN=1 uv run pytest tests/vibesys/golden "
            "and review the new fixture as the diff."
        )
    expected = path.read_text(encoding="utf-8")
    if rendered == expected:
        return
    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=str(path),
            tofile=f"{path} (rendered)",
        )
    )
    pytest.fail(f"Golden snapshot changed: {path}\n{diff}")


def assert_prompt_snapshot(
    strategy: str, role: str, scenario: str, rendered: str, *, workspace: Path | None = None
) -> None:
    """Compare one rendered prompt (system+user text) against its golden file."""
    path = _SNAPSHOT_ROOT / "prompts" / strategy / scenario / f"{role}.txt"
    _assert_matches_snapshot(path, normalize_text(rendered, workspace=workspace))


def assert_board_snapshot(
    strategy: str,
    scenario: str,
    relative_path: str,
    rendered: str,
    *,
    workspace: Path | None = None,
) -> None:
    """Compare one board/memory file's contents against its golden file."""
    path = _SNAPSHOT_ROOT / "board" / strategy / scenario / relative_path
    _assert_matches_snapshot(path, normalize_text(rendered, workspace=workspace))


def assert_events_snapshot(strategy: str, scenario: str, events: list[dict[str, Any]]) -> None:
    """Compare a normalized event-type/key-field sequence against its golden file."""
    path = _SNAPSHOT_ROOT / "events" / strategy / f"{scenario}.json"
    rendered = json.dumps(events, indent=2, sort_keys=False) + "\n"
    _assert_matches_snapshot(path, rendered)


def normalize_event(event: dict[str, Any], *, workspace: Path | None = None) -> dict[str, Any]:
    """Reduce one durable ``CoreEvent`` dict to its deterministic, agent-meaningful fields.

    Drops ``sequence``, ``timestamp``, ``run_id``, and ``execution_id`` (all
    nondeterministic or run-scoped identifiers with no behavioral meaning),
    keeps ``type``/``status``/``round_label``/``agent_kind``/``text``/``data``,
    and normalizes any remaining volatile substrings (SHAs, paths, durations)
    inside the kept fields. Strings longer than ``_EVENT_STRING_LIMIT``
    (full prompts, which the prompt snapshots already hold verbatim) become a
    length and digest marker, so a change still fails the event snapshot
    without duplicating the prompt diff.
    """
    kept = {
        key: event.get(key)
        for key in ("type", "status", "round_label", "agent_kind", "text", "data")
    }
    normalized = json.loads(normalize_text(json.dumps(kept, sort_keys=True), workspace=workspace))
    return _collapse_long_strings(normalized)


_EVENT_STRING_LIMIT = 200


def _collapse_long_strings(value: Any) -> Any:  # noqa: ANN401
    if isinstance(value, str) and len(value) > _EVENT_STRING_LIMIT:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return f"<text len={len(value)} sha256={digest}>"
    if isinstance(value, dict):
        return {key: _collapse_long_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_collapse_long_strings(item) for item in value]
    return value


def _is_diagnostic_log_passthrough(event: dict[str, Any]) -> bool:
    """True for boot-timing/log lines echoed onto the event stream.

    These come from host bootstrap instrumentation (span timings, log
    mirroring), not from orchestration/round behavior, and their content
    and count are sensitive to wall-clock timing -- not to anything this
    golden suite exists to protect against regressing.
    """
    if event.get("type") != "agent_output_chunk":
        return False
    data = event.get("data") or {}
    return data.get("channel") == "diagnostic"


def read_events(events_path: Path, *, workspace: Path | None = None) -> list[dict[str, Any]]:
    """Read every durable core event and normalize it for a golden snapshot.

    Drops diagnostic log-passthrough chunks (see
    :func:`_is_diagnostic_log_passthrough`); keeps every orchestration-
    semantic event (rounds, phases, invocations, gates, judge results).
    """
    if not events_path.exists():
        return []
    events = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if _is_diagnostic_log_passthrough(raw):
            continue
        events.append(normalize_event(raw, workspace=workspace))
    return events


def prompt_text(system_prompt: str, user_prompt: str) -> str:
    """Render one recorded turn's system+user prompt as the snapshot body."""
    return f"# system\n{system_prompt}\n\n# user\n{user_prompt}\n"


def calls_by_kind(client: FakeAgentClient, kind: str) -> list:
    """Return recorded calls for ``kind`` (thin re-export for readability at call sites)."""
    return client.calls_for(kind)
