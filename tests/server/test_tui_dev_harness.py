"""Backend contracts consumed by the TUI replay harness in ``clients/tui/dev``.

The harness is development-only tooling, but its inputs are backend contracts:
its fixtures are recorded ``RunEvent`` journals, and its mock server answers
with hand-written response bodies. The TypeScript client validates neither at
runtime, so a renamed or removed field leaves the harness rendering through its
raw-content fallback while nothing fails.

Everything here validates against the generated protobuf messages, the same
contract the backend client is generated from. Fixtures recorded before the
protobuf wire form are version 1 lines; they are read through the server's own
upgrade, as a journal on disk is.

One test writes rather than validates. The harness reimplements the legacy
translation the server applies when it reads a journal, and the port cannot
import the Python, so ``test_canonical_events_match_the_backend_read_path``
records what the real read path produces and the TypeScript is held to that
file from its own suite.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest

from server.events import parse_event
from server.journal import EventJournal
from server.wire import PROTOCOL_VERSION, codec, upgrade, validate
from server.wire.v2 import events_pb2, requests_pb2, responses_pb2

_HARNESS_DIR = Path(__file__).resolve().parents[2] / "clients" / "tui" / "dev"
_FIXTURE_DIR = _HARNESS_DIR / "fixtures"
_RESPONSES_PATH = _HARNESS_DIR / "mock-responses.json"
_CANONICAL_GOLDEN_PATH = _HARNESS_DIR / "canonical-events.golden.json"

_UPDATE_CANONICAL_ENV = "UPDATE_CANONICAL_EVENTS"
"""Set to ``1`` to rewrite the golden from this read path. Review the diff."""


class SchemaAge(StrEnum):
    """How closely a recorded fixture still matches today's ``RunEvent``."""

    CURRENT = "current"
    """Upgrades and re-parses stably, so it also pins additive drift."""

    LEGACY = "legacy"
    """Validates only. Predates fields the current schema defaults in."""


@dataclass(frozen=True)
class FixtureContract:
    """One recorded fixture and the guarantee it is held to."""

    name: str
    age: SchemaAge
    reason: str


# Every fixture validates. Only a CURRENT one round-trips, and that asymmetry is
# the reason both exist: the current capture catches a field being added, and the
# legacy capture is what proves the renderers still degrade when one is absent.
FIXTURES: tuple[FixtureContract, ...] = (
    FixtureContract(
        name="queue-rs-payloads.jsonl",
        age=SchemaAge.CURRENT,
        reason=(
            "Recorded on the current schema, with a typed payload on every "
            "tool_result. It is the fixture that notices an added field."
        ),
    ),
    FixtureContract(
        name="markdown.jsonl",
        age=SchemaAge.CURRENT,
        reason=(
            "Synthetic, written by markdown.py against the current models "
            "because no real capture contains a table or a fenced code block. "
            "Being generated is why it must round-trip: a drift here means the "
            "generator, not a recording, has gone out of contract."
        ),
    ),
    FixtureContract(
        name="framework-events.jsonl",
        age=SchemaAge.CURRENT,
        reason=(
            "Synthetic, written through the current models because no real "
            "capture yet carries the #692 typed framework events: gate pairs "
            "(including reused and failed), workspace snapshots, run_configured, "
            "and a framework_warning with its lifted diagnostic."
        ),
    ),
    FixtureContract(
        name="bad-cpp-round1.jsonl",
        age=SchemaAge.LEGACY,
        reason=(
            "Recorded before execution_id, chat_thread_id, diagnostic, and typed "
            "tool_result payloads existed, so re-serializing it adds those keys. "
            "Kept for exactly that: it is the harness's only capture of a client "
            "falling back to raw content."
        ),
    ),
)

_CURRENT_FIXTURES = tuple(fixture for fixture in FIXTURES if fixture.age is SchemaAge.CURRENT)

_MISSING = object()
"""Stands in for an absent key, so ``None`` and absent stay distinguishable."""


def _records(fixture: FixtureContract) -> list[tuple[int, dict[str, Any]]]:
    """The fixture's non-empty lines, parsed, with 1-based line numbers."""
    lines = (_FIXTURE_DIR / fixture.name).read_text().splitlines()
    records = [(number, line) for number, line in enumerate(lines, 1) if line.strip()]
    assert records, f"{fixture.name} carries no events"
    return [(number, json.loads(line)) for number, line in records]


def _canonical_events(
    fixture: FixtureContract, tmp_path: Path, through: int
) -> list[events_pb2.RunEvent]:
    """The fixture as a client receives it, through the server's own read path.

    Attached and read rather than canonicalized by hand: ``read`` is the path
    behind every client-facing read, so what this returns is what a subscriber
    would be sent, translation and execution identity included.

    Bounded at the last recorded sequence because attaching to a run is itself
    an event: a fresh journal records its own ``server_started``, which belongs
    to this test's attach and not to the capture.
    """
    log_dir = tmp_path / fixture.name
    log_dir.mkdir(parents=True)
    (log_dir / "run-events.jsonl").write_bytes((_FIXTURE_DIR / fixture.name).read_bytes())
    journal = EventJournal(threading.Condition())
    journal.attach(log_dir, run_id="harness-parity")
    return journal.read(before_sequence=through + 1)


def _canonical_projection(fixture: FixtureContract, tmp_path: Path) -> list[dict[str, Any]]:
    """One canonical JSON object per event a client receives, in order.

    Each entry is ``codec.to_dict`` of the event the real read path returns, so
    the golden pins the version 2 wire form of every event, including the
    upgraded payload, ``execution_id`` recovery, and the agent-execution
    lifecycle a legacy capture is rewritten to.
    """
    records = [record for _number, record in _records(fixture)]
    sequences = [record["sequence"] for record in records]
    assert len(set(sequences)) == len(records), f"{fixture.name} repeats a sequence"
    return [codec.to_dict(event) for event in _canonical_events(fixture, tmp_path, max(sequences))]


def _render_canonical_golden(projected: dict[str, list[dict[str, Any]]]) -> str:
    """One canonical event per line, so the golden diff reads as the stream."""
    blocks = [
        f"  {json.dumps(name)}: [\n    "
        + ",\n    ".join(
            json.dumps(entry, ensure_ascii=False, separators=(",", ":")) for entry in entries
        )
        + "\n  ]"
        for name, entries in projected.items()
    ]
    return "{\n" + ",\n".join(blocks) + "\n}\n"


def _first_difference(expected: list[Any], actual: list[Any]) -> str:
    """Where two projections diverge, as one line rather than 400."""
    for index, (want, got) in enumerate(zip(expected, actual, strict=False)):
        if want != got:
            return f"entry {index}\n  golden: {json.dumps(want)}\n  read path: {json.dumps(got)}"
    return f"length: golden has {len(expected)} entries, the read path {len(actual)}"


def _response_bodies() -> dict[str, dict[str, Any]]:
    """The mock server's static response bodies, keyed by request type."""
    bodies: dict[str, dict[str, Any]] = json.loads(_RESPONSES_PATH.read_text())
    return bodies


def _request_types() -> set[str]:
    """Every body a client request may carry."""
    return {field.name for field in requests_pb2.Request.DESCRIPTOR.oneofs_by_name["body"].fields}


def _request_type(key: str) -> str:
    """The request body a mock-response key answers (``query.snapshot`` is ``snapshot``)."""
    return key.rpartition(".")[2]


def test_every_fixture_declares_a_schema_guarantee() -> None:
    """An undeclared fixture would be replayed by the harness and checked by nothing.

    Only replayable journals are declared. `markdown.py`, the generator that
    writes one of them, sits in the same directory, and the harness's other data
    files, the static response bodies and the canonicalization golden, sit one
    level up in `dev/`. None of the three carries a `RunEvent`.
    """
    present = {
        path.name
        for path in _FIXTURE_DIR.iterdir()
        if path.is_file() and path.name.endswith((".jsonl", ".jsonl.gz"))
    }
    assert present == {fixture.name for fixture in FIXTURES}


def test_some_fixture_carries_the_current_schema() -> None:
    """Grandfathering every fixture as LEGACY would silently drop the round trip."""
    assert _CURRENT_FIXTURES


def _parse(record: dict[str, Any]) -> events_pb2.RunEvent:
    """Parse one fixture line as the journal reader does: upgrade a legacy record."""
    return parse_event(json.dumps(record))


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.name)
def test_fixture_validates_as_run_events(fixture: FixtureContract) -> None:
    """Catches a breaking change: a renamed or dropped field, or an unknown kind."""
    for number, record in _records(fixture):
        try:
            _parse(record)
        except codec.WireError as error:
            # Reported outside the handler so the report is the message alone,
            # not the message chained onto the same error's traceback.
            failure = f"{fixture.name}:{number} is no longer a RunEvent:\n{error}"
        else:
            continue
        pytest.fail(failure, pytrace=False)


@pytest.mark.parametrize("fixture", _CURRENT_FIXTURES, ids=lambda fixture: fixture.name)
def test_current_fixture_round_trips_exactly(fixture: FixtureContract) -> None:
    """Catches additive drift, which validation alone accepts, and names the key.

    A version 2 line must re-serialize to itself. A version 1 line cannot (the
    upgrade drops nulls and renames enums), so its upgraded event must instead
    survive a serialize and parse cycle unchanged.
    """
    for number, record in _records(fixture):
        event = _parse(record)
        dumped = codec.to_dict(event)
        expected = dumped if upgrade.is_legacy(record) else record
        if codec.from_dict(events_pb2.RunEvent, dumped) == event and dumped == expected:
            continue
        drifted = sorted(
            key
            for key in dumped.keys() | expected.keys()
            if dumped.get(key, _MISSING) != expected.get(key, _MISSING)
        )
        pytest.fail(
            f"{fixture.name}:{number} no longer re-serializes to itself; "
            f"drifted keys: {', '.join(drifted)}. Re-record the fixture, or "
            f"declare it {SchemaAge.LEGACY.name} if the harness should keep "
            f"replaying the older shape.",
            pytrace=False,
        )


def test_canonical_events_match_the_backend_read_path(tmp_path: Path) -> None:
    """Holds the harness's hand-ported translation to the read path it copies.

    `clients/tui/dev/journal.ts` reimplements what `EventJournal` does to a
    legacy journal on read: the version 1 to version 2 upgrade, `execution_id`
    recovered from `invocation_id`, and
    `invocation_started`/`invocation_finished` rewritten to their
    `agent_execution_*` form. Without it the harness was the one place a client
    met an untranslated legacy line, and `bad-cpp-round1.jsonl` replayed with no
    agent executions at all.

    The port cannot import the Python, so this writes what the Python produces
    and the parity test in `harness.test.ts` holds the TypeScript to the same
    file. A change to either side alone fails one of the two. This half is here
    because only here can the real adapter be called.

    Regenerate with `UPDATE_CANONICAL_EVENTS=1 uv run pytest
    tests/server/test_tui_dev_harness.py`, then re-run the TUI suite: a golden
    that moved on its own is a backend change the harness has not been taught.
    """
    projected = {fixture.name: _canonical_projection(fixture, tmp_path) for fixture in FIXTURES}
    if os.environ.get(_UPDATE_CANONICAL_ENV) == "1":
        _CANONICAL_GOLDEN_PATH.write_text(_render_canonical_golden(projected), encoding="utf-8")
        return
    golden: dict[str, list[dict[str, Any]]] = json.loads(
        _CANONICAL_GOLDEN_PATH.read_text(encoding="utf-8")
    )
    assert golden.keys() == projected.keys()
    for name, entries in projected.items():
        if golden[name] == entries:
            continue
        pytest.fail(
            f"{_CANONICAL_GOLDEN_PATH.name} no longer describes how the read path "
            f"canonicalizes {name}: {_first_difference(golden[name], entries)}",
            pytrace=False,
        )


def test_mock_response_bodies_are_keyed_by_request_type() -> None:
    """A typo in a key would leave that body unreachable and unvalidated."""
    assert {_request_type(key) for key in _response_bodies()} <= _request_types()


@pytest.mark.parametrize("request_type", sorted(_response_bodies()))
def test_mock_response_body_is_a_valid_response(request_type: str) -> None:
    """Holds ``mock-server.ts`` to the wire contract it hand-writes answers for.

    This validates the file the mock server reads its bodies from, so what it
    checks is what goes on the wire. It cannot check the values the server
    substitutes at runtime (run id, sequence, status, theme, backfilled events),
    because CI's pytest job has no JavaScript runtime to run the harness under;
    ``withValues`` in ``mock-server.ts`` guards those by rejecting any key this
    file does not already carry.
    """
    body = _response_bodies()[request_type]
    envelope = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": "schema-check",
        "ok": True,
        **body,
    }
    validate.validate_message(codec.from_dict(responses_pb2.Response, envelope))
